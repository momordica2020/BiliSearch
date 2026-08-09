/* 浏览器环境模拟：验证引擎 load() 能从真实分片 URL 加载索引。
   修复过 "Invalid base URL"（相对路径 base 未解析）回归。
   用法：node scripts/verify_load.mjs */
import { createServer } from "node:http";
import { createReadStream, statSync } from "node:fs";
import { join, normalize } from "node:path";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { BiliSearchEngine, typeName, urlOf } = require("../site/search.js");

const root = normalize(join(import.meta.dirname, "..", "site"));
const port = 8123;

const server = createServer((req, res) => {
  const urlPath = new URL(req.url, `http://127.0.0.1:${port}`).pathname;
  const file = normalize(join(root, urlPath === "/" ? "index.html" : urlPath));
  if (!file.startsWith(root)) {
    res.writeHead(403); res.end("forbidden"); return;
  }
  try {
    const stat = statSync(file);
    res.writeHead(200, {
      "Content-Type": urlPath.endsWith(".gz") ? "application/gzip"
        : urlPath.endsWith(".json") ? "application/json" : "text/plain",
      "Content-Length": stat.size,
    });
    createReadStream(file).pipe(res);
  } catch {
    res.writeHead(404); res.end("not found");
  }
});

server.listen(port, "127.0.0.1", async () => {
  try {
    globalThis.location = { href: `http://127.0.0.1:${port}/index.html` };
    const engine = new BiliSearchEngine();
    const meta = await engine.load(
      new URL("data/meta.json", globalThis.location.href).href,
      (done, total) => {
      console.log(`分片 ${done}/${total}`);
      }
    );
    const { total, items } = engine.search("老番茄", { limit: 3 });
    console.log(`load OK：${meta.total} 条，查询「老番茄」→ ${total} 条`);
    for (const it of items) console.log(`  [${typeName(it.t)}] ${it.s} | ${urlOf(it)}`);
  } catch (e) {
    console.error("load FAIL:", e.message);
    process.exitCode = 1;
  } finally {
    server.close();
  }
});
