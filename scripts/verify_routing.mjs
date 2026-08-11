/* 路由模式端到端验证：HTTP 服务 + RoutingEngine，观察每次查询下载多少分片。
   用法：node scripts/verify_routing.mjs [查询词...] */
import { createServer } from "node:http";
import { createReadStream, statSync } from "node:fs";
import { join, normalize } from "node:path";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { BiliSearchRouting, typeName } = require("../site/search.js");

const root = normalize(join(import.meta.dirname, "..", "site"));
const port = 8124;
const server = createServer((req, res) => {
  const urlPath = new URL(req.url, `http://127.0.0.1:${port}`).pathname;
  const file = normalize(join(root, urlPath === "/" ? "index.html" : urlPath));
  if (!file.startsWith(root)) { res.writeHead(403); res.end(); return; }
  try {
    const stat = statSync(file);
    res.writeHead(200, {
      "Content-Type": urlPath.endsWith(".gz") ? "application/gzip" : "application/json",
      "Content-Length": stat.size,
    });
    createReadStream(file).pipe(res);
  } catch { res.writeHead(404); res.end("not found"); }
});

server.listen(port, "127.0.0.1", async () => {
  try {
    globalThis.location = { href: `http://127.0.0.1:${port}/index.html` };
    const engine = new BiliSearchRouting();
    const meta = await engine.load(new URL("data/meta.json", location.href).href);
    console.log(`路由模式加载：${meta.total} 条 | 数据分片 ${meta.shards.length} | 目录分片 ${meta.dirShards.length}`);
    const queries = process.argv.length > 2 ? process.argv.slice(2) : ["老番茄", "rick", "美人鱼", "周处除三害"];
    for (const q of queries) {
      const t0 = Date.now();
      const res = await engine.search(q, { limit: 5 });
      const ms = Date.now() - t0;
      console.log(`\n「${q}」→ ${res.total} 条命中 | 检索分片 ${res.scanned}/${res.candidates} | 下载 ${(res.bytes / 1048576).toFixed(2)}MB | ${ms}ms${res.partial ? "（部分检索）" : ""}`);
      for (const it of res.items) {
        console.log(`  [${typeName(it.t)}] ${it.s} | ${it.a} | 相关度 ${it.score}`);
      }
    }
  } catch (e) {
    console.error("FAIL:", e.message);
    process.exitCode = 1;
  } finally {
    server.close();
  }
});
