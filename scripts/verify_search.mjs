/* 本地验证：加载构建好的分片，跑几个查询看结果。用法：
   node scripts/verify_search.mjs [site/data] [查询词...] */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { gunzipSync } from "node:zlib";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { BiliSearchEngine } = require("../site/search.js");

const dataDir = process.argv[2] || "site/data";
const meta = JSON.parse(readFileSync(join(dataDir, "meta.json"), "utf8"));
const engine = new BiliSearchEngine();
for (const sh of meta.shards) {
  const text = gunzipSync(readFileSync(join(dataDir, sh.url))).toString("utf8");
  for (const line of text.split("\n")) {
    if (line.trim()) engine.addDoc(JSON.parse(line));
  }
}
console.log(`已加载 ${meta.total} 条 / ${meta.shards.length} 分片`);

const queries = process.argv.length > 3 ? process.argv.slice(3) : ["测试", "bilibili"];
for (const q of queries) {
  const { total, items } = engine.search(q, { limit: 5 });
  console.log(`\n查询「${q}」→ ${total} 条`);
  for (const it of items) {
    console.log(`  [${it.t}] ${it.s} | ${it.a} | ${it.l} (相关度 ${it.score})`);
  }
}

