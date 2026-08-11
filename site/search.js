/*
 * BiliSearch 客户端搜索引擎（零依赖）。
 * 索引在浏览器端构建：CJK 二元组 + 拉丁词元倒排表；
 * 查询支持精确词、前缀、编辑距离（模糊）、单汉字扩散匹配。
 * 可在浏览器（<script>）或 Node（require）中使用。
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.BiliSearch = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const CJK = /[\u3400-\u9fff]/;
  const WORD = /[a-z0-9][a-z0-9._#+\-]*/g;
  const TYPES = ["video", "user", "dynamic", "article"];

  function tokenize(text) {
    const tokens = [];
    const s = String(text == null ? "" : text).toLowerCase();
    for (const m of s.matchAll(WORD)) tokens.push(m[0]);
    const cjk = [];
    for (const ch of s) if (CJK.test(ch)) cjk.push(ch);
    for (let i = 0; i + 1 < cjk.length; i++) tokens.push(cjk[i] + cjk[i + 1]);
    if (cjk.length === 1) tokens.push(cjk[0]);
    return tokens;
  }

  function levenshtein(a, b, cap) {
    if (Math.abs(a.length - b.length) > cap) return cap + 1;
    if (a === b) return 0;
    let prev = new Array(b.length + 1);
    let cur = new Array(b.length + 1);
    for (let j = 0; j <= b.length; j++) prev[j] = j;
    for (let i = 1; i <= a.length; i++) {
      cur[0] = i;
      for (let j = 1; j <= b.length; j++) {
        const cost = a[i - 1] === b[j - 1] ? 0 : 1;
        cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost);
      }
      if (Math.min.apply(null, cur) > cap) return cap + 1;
      const tmp = prev; prev = cur; cur = tmp;
    }
    return prev[b.length];
  }

  async function fetchText(url) {
    const res = await fetch(url, { cache: "no-store" });
    if (!res.ok) throw new Error("HTTP " + res.status + " " + url);
    if (/\.gz($|\?)/.test(url)) {
      if (typeof DecompressionStream === "undefined") {
        throw new Error("当前浏览器不支持 gzip 解压（需 Chrome/Edge≥102、Firefox≥113、Safari≥16.4）");
      }
      const buf = await res.arrayBuffer();
      const stream = new Response(
        new Blob([buf]).stream().pipeThrough(new DecompressionStream("gzip"))
      );
      return await stream.text();
    }
    return await res.text();
  }

  async function poolMap(items, limit, fn) {
    const out = new Array(items.length);
    let next = 0;
    async function worker() {
      while (next < items.length) {
        const i = next++;
        out[i] = await fn(items[i], i);
      }
    }
    await Promise.all(Array.from({ length: Math.min(limit, items.length) }, () => worker()));
    return out;
  }

  function addTo(map, token, docIdx, weight) {
    let m = map.get(token);
    if (!m) {
      m = new Map();
      map.set(token, m);
    }
    m.set(docIdx, (m.get(docIdx) || 0) + weight);
  }

  function typeName(t) {
    return TYPES[t] || String(t);
  }

  // URL 全部由 id 客户端推导，索引里不再存链接（省约 30% 体积）
  function urlOf(doc) {
    const id = String(doc.i || "");
    switch (typeName(doc.t)) {
      case "video":
        return "https://www.bilibili.com/video/" + id;
      case "user":
        return "https://space.bilibili.com/" + id.replace(/^mid:/, "");
      case "dynamic":
        return "https://t.bilibili.com/" + id.replace(/^dyn:/, "");
      case "article":
        return "https://www.bilibili.com/read/" + (id.startsWith("cv") ? id : "cv" + id);
      default:
        return "";
    }
  }

  class BiliSearchEngine {
    constructor() {
      this.docs = [];
      this.idx = new Map();
      this.titleIdx = new Map();
      this.meta = null;
    }

    reset() {
      this.docs = [];
      this.idx = new Map();
      this.titleIdx = new Map();
      this.meta = null;
    }

    addDoc(doc) {
      const i = this.docs.length;
      this.docs.push(doc);
      const fields = [
        [doc.s, 2.0, this.titleIdx],   // 标题（另建 title 索引用于加权）
        [doc.a, 1.5, null],
        [doc.c, 1.2, null],
        [doc.d, 1.0, null],
      ];
      for (const [text, w, titleMap] of fields) {
        const seen = new Set();
        for (const tok of tokenize(text)) {
          if (seen.has(tok)) continue;
          seen.add(tok);
          addTo(this.idx, tok, i, w);
          if (titleMap) addTo(titleMap, tok, i, 1);
        }
      }
    }

    async load(metaUrl, onProgress) {
      this.reset();
      const meta = JSON.parse(await fetchText(metaUrl));
      if (!Array.isArray(meta.shards) || !meta.shards.length) {
        throw new Error("meta.json 中没有可加载的分片（请先运行 build_index.py）");
      }
      this.meta = meta;
      // metaUrl 可能是相对路径（如 data/meta.json），先基于页面地址解析成绝对 URL，
      // 否则 new URL(shard, metaUrl) 在浏览器会抛 "Invalid base URL"。
      const base = new URL(
        metaUrl,
        typeof location !== "undefined" && location.href
          ? location.href
          : "http://localhost/"
      );
      const total = meta.shards.length;
      let done = 0;
      for (const sh of meta.shards) {
        const body = await fetchText(new URL(sh.url, base).href);
        for (const line of body.split("\n")) {
          const t = line.trim();
          if (!t) continue;
          try {
            this.addDoc(JSON.parse(t));
          } catch (_) { /* 坏行跳过 */ }
        }
        done += 1;
        if (onProgress) onProgress(done, total);
      }
      return meta;
    }

    _apply(map, token, weight, scores, wantTypes, boostTitle) {
      const m = map.get(token);
      if (!m) return;
      for (const [di, w] of m) {
        if (wantTypes && !wantTypes.has(this.docs[di].t)) continue;
        let s = (scores.get(di) || 0) + w * weight;
        if (boostTitle) {
          const tm = this.titleIdx.get(token);
          if (tm && tm.has(di)) s += tm.get(di) * weight * 0.6;
        }
        scores.set(di, s);
      }
    }

    search(query, opts = {}) {
      const q = String(query || "").trim().toLowerCase();
      if (!q) return { total: 0, items: [] };
      const fuzzy = opts.fuzzy == null ? 1 : opts.fuzzy;
      const limit = opts.limit || 50;
      const offset = opts.offset || 0;
      const qTokens = tokenize(q);
      const scores = new Map();
      const wantTypes = Array.isArray(opts.types) && opts.types.length
        ? new Set(opts.types.map((t) => TYPES.indexOf(t)))
        : null;

      const allowed = (docT) => !wantTypes || wantTypes.has(docT);

      for (const tok of qTokens) {
        if (tok.length === 1 && CJK.test(tok)) {
          // 单汉字：扩散匹配所有包含该字的索引词
          for (const [key, m] of this.idx) {
            if (!key.includes(tok)) continue;
            for (const [di, w] of m) {
              if (!allowed(this.docs[di].t)) continue;
              scores.set(di, (scores.get(di) || 0) + w * 0.6);
            }
          }
          continue;
        }
        this._apply(this.idx, tok, 2.2, scores, wantTypes, true);
        if (/[a-z0-9]/.test(tok)) {
          if (tok.length >= 2) {
            for (const key of this.idx.keys()) {
              if (key.length > tok.length && key.startsWith(tok)) {
                this._apply(this.idx, key, 1.1, scores, wantTypes, false);
              }
            }
          }
          const distCap = fuzzy >= 2 && tok.length >= 6 ? 2 : 1;
          if (tok.length >= 4) {
            for (const key of this.idx.keys()) {
              if (Math.abs(key.length - tok.length) > distCap) continue;
              if (levenshtein(key, tok, distCap) <= distCap) {
                this._apply(this.idx, key, 0.9, scores, wantTypes, false);
              }
            }
          }
        }
      }

      const rows = [];
      for (const [di, s] of scores) {
        rows.push({ doc: this.docs[di], score: s });
      }
      rows.sort((a, b) => b.score - a.score || (b.doc.p || 0) - (a.doc.p || 0));
      const total = rows.length;
      const items = rows.slice(offset, offset + limit).map((r) => ({
        ...r.doc,
        score: Math.round(r.score * 10) / 10,
      }));
      return { total, items };
    }
  }

  function scoreDoc(d, tokens) {
    let s = 0;
    const fields = [[d.s, 2], [d.a, 1.5], [d.c, 1.2], [d.d, 1]];
    for (const [text, w] of fields) {
      const seen = new Set();
      for (const t of tokenize(text)) {
        if (seen.has(t)) continue;
        seen.add(t);
        if (tokens.has(t)) s += w;
      }
    }
    for (const t of tokens) {
      if (t.length === 1 && CJK.test(t)) {
        if ((d.s || "").includes(t)) s += 1.5;
        if ((d.a || "").includes(t)) s += 1;
      }
    }
    return s;
  }

  class BiliSearchRouting {
    /* 路由模式：查询时只下载“目录分片 + 相关数据分片”，不加载全量。
       数据分片可托管在多个仓库/分支（meta.shards[].url 支持绝对地址）。 */
    constructor() {
      this.meta = null;
      this.metaUrl = "";
      this.dirCache = new Map();
      this.dataCache = new Map();
    }

    async load(metaUrl, onProgress) {
      this.metaUrl = metaUrl;
      this.meta = JSON.parse(await fetchText(metaUrl));
      if (this.meta.type !== "routing") {
        throw new Error("meta.json 不是路由模式（type != routing）");
      }
      if (onProgress) onProgress(1, 1);
      return this.meta;
    }

    _dirShardsForToken(tok) {
      return this.meta.dirShards.filter((d) => tok >= d.min && tok <= d.max);
    }

    _dirShardsForRange(lo, hi) {
      return this.meta.dirShards.filter((d) => d.max >= lo && d.min <= hi);
    }

    async _loadDir(id) {
      if (this.dirCache.has(id)) return this.dirCache.get(id);
      const sh = this.meta.dirShards[id];
      const text = await fetchText(new URL(sh.url, this.metaUrl).href);
      const map = new Map();
      for (const line of text.split("\n")) {
        const t = line.trim();
        if (!t) continue;
        const tab = t.indexOf("\t");
        if (tab < 0) continue;
        const arr = [];
        for (const pair of t.slice(tab + 1).split(",")) {
          const c = pair.indexOf(":");
          if (c > 0) arr.push({ cnt: +pair.slice(0, c), shard: +pair.slice(c + 1) });
        }
        map.set(t.slice(0, tab), arr);
      }
      this.dirCache.set(id, map);
      return map;
    }

    async _collect(tok) {
      const isSingleCjk = tok.length === 1 && CJK.test(tok);
      const expandPrefix = isSingleCjk || (/[a-z0-9]/.test(tok) && tok.length >= 3);
      const dirs = isSingleCjk
        ? this._dirShardsForRange(tok, tok + "\uffff")
        : expandPrefix
          ? this._dirShardsForRange(tok, tok + "\uffff")
          : this._dirShardsForToken(tok);
      const out = new Map();
      let loaded = 0;
      for (const d of dirs) {
        if (loaded >= 12) break;
        const m = await this._loadDir(d.id);
        loaded++;
        if (!expandPrefix) {
          const arr = m.get(tok);
          if (arr) for (const { cnt, shard } of arr) {
            out.set(shard, (out.get(shard) || 0) + cnt);
          }
          continue;
        }
        for (const [key, arr] of m) {
          if (isSingleCjk ? key[0] === tok : (key.length >= tok.length && key.startsWith(tok))) {
            for (const { cnt, shard } of arr) {
              out.set(shard, (out.get(shard) || 0) + cnt);
            }
          }
        }
      }
      return out;
    }

    async search(query, opts = {}) {
      const q = String(query || "").trim();
      if (!q) return { total: 0, items: [], partial: false, scanned: 0, candidates: 0, bytes: 0 };
      const qTokens = [...new Set(tokenize(q))];
      const wantTypes = Array.isArray(opts.types) && opts.types.length
        ? new Set(opts.types.map((t) => TYPES.indexOf(t))) : null;
      const budgetBytes = opts.budgetBytes || this.meta.search.budgetBytes || 24000000;
      const maxShards = opts.maxShards || this.meta.search.maxShards || 64;
      const limit = opts.limit || 50;

      const maps = [];
      for (const tok of qTokens) maps.push(await this._collect(tok));
      if (!maps.length) return { total: 0, items: [], partial: false, scanned: 0, candidates: 0, bytes: 0 };

      let primary = 0;
      maps.forEach((m, i) => { if (m.size < maps[primary].size) primary = i; });
      let cand = new Set(maps[primary].keys());
      for (let i = 0; i < maps.length; i++) {
        if (i === primary) continue;
        const inter = new Set([...cand].filter((s) => maps[i].has(s)));
        if (inter.size) cand = inter;
      }
      const candArr = [...cand]
        .map((shard) => [shard, maps.reduce((w, m) => w + (m.get(shard) || 0), 0)])
        .sort((a, b) => b[1] - a[1]);

      const tokens = new Set(qTokens);
      const picked = candArr.slice(0, maxShards);
      const results = [];
      let bytes = 0;
      let scanned = 0;
      const byId = new Map(this.meta.shards.map((s) => [s.id, s]));
      const texts = await poolMap(picked, 6, async ([shardId]) => {
        const sh = byId.get(shardId);
        if (!sh) return null;
        const url = new URL(sh.url, this.metaUrl).href;
        if (!this.dataCache.has(url)) {
          this.dataCache.set(url, await fetchText(url));
          if (this.dataCache.size > 128) {
            const k = this.dataCache.keys().next().value;
            this.dataCache.delete(k);
          }
        }
        return { sh, text: this.dataCache.get(url) };
      });
      for (const item of texts) {
        if (!item) continue;
        const { sh, text } = item;
        if (bytes + sh.bytes > budgetBytes && results.length) break;
        bytes += sh.bytes;
        scanned++;
        for (const line of text.split("\n")) {
          const t = line.trim();
          if (!t) continue;
          let d;
          try { d = JSON.parse(t); } catch { continue; }
          if (wantTypes && !wantTypes.has(d.t)) continue;
          const sc = scoreDoc(d, tokens);
          if (sc > 0) results.push({ d, sc });
        }
      }
      results.sort((a, b) => b.sc - a.sc || (b.d.p || 0) - (a.d.p || 0));
      const items = results.slice(0, limit).map((r) => ({
        ...r.d,
        score: Math.round(r.sc * 10) / 10,
      }));
      return {
        total: results.length,
        items,
        partial: scanned < candArr.length,
        scanned,
        candidates: candArr.length,
        bytes,
      };
    }
  }

  return { BiliSearchEngine, BiliSearchRouting, tokenize, levenshtein, fetchText, typeName, urlOf };
});
