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

  function addTo(map, token, docIdx, weight) {
    let m = map.get(token);
    if (!m) {
      m = new Map();
      map.set(token, m);
    }
    m.set(docIdx, (m.get(docIdx) || 0) + weight);
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

    _apply(map, token, weight, scores, typesSet, boostTitle) {
      const m = map.get(token);
      if (!m) return;
      for (const [di, w] of m) {
        if (typesSet && !typesSet.has(this.docs[di].t)) continue;
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
      const types = Array.isArray(opts.types) && opts.types.length
        ? new Set(opts.types) : null;
      const fuzzy = opts.fuzzy == null ? 1 : opts.fuzzy;
      const limit = opts.limit || 50;
      const offset = opts.offset || 0;
      const qTokens = tokenize(q);
      const scores = new Map();

      for (const tok of qTokens) {
        if (tok.length === 1 && CJK.test(tok)) {
          // 单汉字：扩散匹配所有包含该字的索引词
          for (const [key, m] of this.idx) {
            if (!key.includes(tok)) continue;
            for (const [di, w] of m) {
              if (typesSet2(types, this.docs[di].t)) continue;
              scores.set(di, (scores.get(di) || 0) + w * 0.6);
            }
          }
          continue;
        }
        this._apply(this.idx, tok, 2.2, scores, types, true);
        if (/[a-z0-9]/.test(tok)) {
          if (tok.length >= 2) {
            for (const key of this.idx.keys()) {
              if (key.length > tok.length && key.startsWith(tok)) {
                this._apply(this.idx, key, 1.1, scores, types, false);
              }
            }
          }
          const distCap = fuzzy >= 2 && tok.length >= 6 ? 2 : 1;
          if (tok.length >= 4) {
            for (const key of this.idx.keys()) {
              if (Math.abs(key.length - tok.length) > distCap) continue;
              if (levenshtein(key, tok, distCap) <= distCap) {
                this._apply(this.idx, key, 0.9, scores, types, false);
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

  function typesSet2(set, t) {
    return set && !set.has(t);
  }

  return { BiliSearchEngine, tokenize, levenshtein, fetchText };
});
