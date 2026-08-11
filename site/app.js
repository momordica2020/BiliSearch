(function () {
  "use strict";

  const TYPE_LABEL = { video: "视频", user: "UP主", dynamic: "动态", article: "专栏" };
  let engine = null;
  let routing = false;

  const els = {
    input: document.getElementById("q"),
    tabs: document.querySelectorAll(".tab"),
    sort: document.getElementById("sort"),
    status: document.getElementById("status"),
    progress: document.getElementById("progress"),
    stats: document.getElementById("stats"),
    results: document.getElementById("results"),
    more: document.getElementById("more"),
    empty: document.getElementById("empty"),
  };

  const state = { q: "", type: "all", sort: "relevance", page: 1, pageSize: 50, rows: [] };
  let debounce = null;
  let ready = false;
  let searchSeq = 0;

  function init() {
    els.input.addEventListener("input", () => {
      clearTimeout(debounce);
      debounce = setTimeout(() => {
        state.q = els.input.value.trim();
        state.page = 1;
        writeHash();
        runSearch();
      }, 220);
    });
    els.tabs.forEach((tab) => {
      tab.addEventListener("click", () => {
        state.type = tab.dataset.type;
        state.page = 1;
        els.tabs.forEach((t) => t.classList.toggle("active", t === tab));
        writeHash();
        runSearch();
      });
    });
    els.sort.addEventListener("change", () => {
      state.sort = els.sort.value;
      state.page = 1;
      writeHash();
      runSearch();
    });
    els.more.addEventListener("click", () => {
      state.page += 1;
      runSearch();
    });
    readHash();
    els.input.value = state.q;
    registerSW();
    window.addEventListener("online", updateNet);
    window.addEventListener("offline", updateNet);
    loadIndex();
  }

  function readHash() {
    const p = new URLSearchParams(location.hash.slice(1));
    state.q = p.get("q") || "";
    state.type = p.get("t") || "all";
    state.sort = p.get("s") || "relevance";
    els.tabs.forEach((t) => t.classList.toggle("active", t.dataset.type === state.type));
    els.sort.value = state.sort;
  }

  function writeHash() {
    const p = new URLSearchParams();
    if (state.q) p.set("q", state.q);
    if (state.type !== "all") p.set("t", state.type);
    if (state.sort !== "relevance") p.set("s", state.sort);
    history.replaceState(null, "", "#" + p.toString());
  }

  async function loadIndex() {
    setLoading("正在下载索引…");
    try {
      const metaUrl = new URL("data/meta.json", location.href).href;
      const meta = JSON.parse(await BiliSearch.fetchText(metaUrl));
      routing = meta.type === "routing";
      engine = routing
        ? new BiliSearch.BiliSearchRouting()
        : new BiliSearch.BiliSearchEngine();
      if (routing) {
        engine.metaUrl = metaUrl;
        engine.meta = meta;
      } else {
        await engine.load(metaUrl, (done, total) => {
          setLoading("正在下载索引 " + done + "/" + total + " 分片…");
        });
      }
      ready = true;
      setStats(meta);
      updateNet();
      const mode = routing ? "路由搜索模式" : "内存索引模式";
      setLoading(mode + " · " + new Date().toLocaleTimeString("zh-CN", { hour12: false }));
      if (state.q) runSearch();
      else showEmpty("输入关键词开始搜索，例如：某个视频标题、UP 主昵称、专栏关键词。");
    } catch (e) {
      setLoading("索引加载失败：" + e.message);
      showEmpty("请确认 site/data/ 已由 build_index.py 生成并已部署，且浏览器支持 DecompressionStream。");
    }
  }

  function setStats(meta) {
    const c = meta.counts || {};
    const parts = ["共 " + (meta.total || 0).toLocaleString() + " 条"];
    for (const t of Object.keys(TYPE_LABEL)) {
      if (c[t]) parts.push(TYPE_LABEL[t] + " " + c[t].toLocaleString());
    }
    els.stats.textContent = parts.join(" · ");
    els.tabs.forEach((tab) => {
      const n = c[tab.dataset.type] || 0;
      const label = tab.textContent.split(" ")[0];
      tab.textContent = label + (n ? " " + n.toLocaleString() : "");
    });
  }

  async function runSearch() {
    if (!ready) return;
    const types = state.type === "all" ? null : [state.type];
    const seq = ++searchSeq;
    let res;
    try {
      res = await engine.search(state.q, { types, limit: state.page * state.pageSize });
    } catch (e) {
      setLoading("搜索失败：" + e.message);
      return;
    }
    if (seq !== searchSeq) return;  // 丢弃过期结果
    const total = res.total;
    let items = res.items;
    if (state.sort === "date") {
      items = items.slice().sort((a, b) => (b.p || 0) - (a.p || 0));
    }
    state.rows = items;
    render(items);
    els.more.style.display = total > items.length ? "block" : "none";
    if (routing) {
      const totalShards = engine.meta.shardCount || engine.meta.shards.length;
      const pct = res.candidates ? Math.round((res.scanned / res.candidates) * 100) : 100;
      const cov = res.partial
        ? `已检索 ${res.scanned}/${res.candidates} 候选分片（${pct}%，结果可能不完整）· 全库 ${totalShards} 分片`
        : `已检索 ${res.scanned}/${res.candidates} 候选分片 · 全库 ${totalShards} 分片`;
      setLoading("搜索完成 · " + cov + (res.bytes ? " · " + (res.bytes / 1048576).toFixed(1) + "MB" : ""));
    }
  }

  function render(rows) {
    els.empty.style.display = "none";
    els.results.innerHTML = rows.map(itemHTML).join("");
    if (!rows.length) showEmpty("没有匹配的结果，试试更短的关键词。");
  }

  function showEmpty(text) {
    els.empty.textContent = text;
    els.empty.style.display = "block";
    els.results.innerHTML = "";
  }

  function itemHTML(d) {
    const tname = BiliSearch.typeName(d.t);
    const label = TYPE_LABEL[tname] || tname;
    const date = d.p ? fmtDate(d.p) : "";
    const meta = [d.a, d.c, date, "相关度 " + (d.score || 0)].filter(Boolean).join(" · ");
    const desc = d.d ? '<div class="res-desc">' + esc(d.d) + "</div>" : "";
    return (
      '<a class="res" href="' + esc(BiliSearch.urlOf(d)) + '" target="_blank" rel="noopener">' +
      '<div class="res-top"><span class="badge t-' + esc(tname) + '">' + label + "</span>" +
      '<span class="res-title">' + esc(d.s) + "</span></div>" +
      '<div class="res-meta">' + esc(meta) + "</div>" + desc +
      "</a>"
    );
  }

  function setLoading(text) {
    els.status.textContent = text;
  }

  function updateNet() {
    const mode = navigator.onLine ? "" : " · 离线模式（使用缓存索引）";
    if (ready) setLoading("索引就绪" + mode);
  }

  function registerSW() {
    if (!("serviceWorker" in navigator)) return;
    navigator.serviceWorker.register("sw.js").catch((e) => console.warn("SW:", e));
  }

  function fmtDate(ts) {
    const d = new Date(ts * 1000);
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") +
      "-" + String(d.getDate()).padStart(2, "0");
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  document.addEventListener("DOMContentLoaded", init);
})();
