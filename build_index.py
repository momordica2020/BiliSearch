#!/usr/bin/env python3
"""把 data/raw/*.jsonl 构建成静态站可用的紧凑 gzip 分片索引。

输出到 site/data/：
    meta.json          —— 版本、构建时间、各类型数量、分片清单
    shards/NN-<hash>.jsonl.gz —— 每条记录一行紧凑 JSON，键省略空字段：
        {"i","t","s","a","u","d","p","c"}，t 为类型数字码，URL 由 id 客户端推导

默认“稳定分桶”：按 (type:id) 哈希分到固定数量的桶，新增内容不会移动旧分片；
文件名带内容哈希，未变化的分片文件保持不变，git 不会重复提交历史。

分片可独立托管：把 meta.json 里的 shards[].url 指到其他分支/仓库的
raw.githubusercontent.com 地址即可横向扩容（见 README“多分支/多仓库”）。
"""

import argparse
import gzip
import hashlib
import json
import sys
import time
from pathlib import Path

RAW_FILES = ["videos.jsonl", "users.jsonl", "dynamics.jsonl", "articles.jsonl"]
TYPE_NAMES = {"video": "视频", "user": "UP主", "dynamic": "动态", "article": "专栏"}
TYPE_CODE = {"video": 0, "user": 1, "dynamic": 2, "article": 3}


def load_records(raw_dir: Path):
    """按 (type, id) 去重，后写入的覆盖先写入的；保留最少字段。"""
    records = {}
    for fname in RAW_FILES:
        p = raw_dir / fname
        if not p.exists():
            continue
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not r.get("title"):
                    continue
                records[(r.get("type"), r.get("id"))] = r
    return list(records.values())


def compact(rec):
    out = {"i": rec.get("id", ""), "t": TYPE_CODE.get(rec.get("type"), 0)}
    out["s"] = rec.get("title", "")
    if rec.get("author"):
        out["a"] = rec["author"]
    if rec.get("author_id"):
        out["u"] = rec["author_id"]
    d = (rec.get("desc") or "")[:args_desc_len]
    if d:
        out["d"] = d
    p = int(rec.get("pubdate") or 0)
    if p:
        out["p"] = p
    if rec.get("category"):
        out["c"] = rec["category"]
    return out


args_desc_len = 180  # 会被 main 覆盖


def write_shards(records, out_dir: Path, shard_size: int, shard_count: int):
    shard_dir = out_dir / "shards"
    if shard_dir.exists():
        # 清理旧分片，避免残留过期 shard 被站点加载
        for old in shard_dir.glob("*.jsonl.gz"):
            old.unlink()
    shard_dir.mkdir(parents=True, exist_ok=True)
    shards = []
    if shard_count:
        # 稳定分桶：哈希取模，新纪录只进自己的桶，旧分片内容与文件名均不变
        buckets = [[] for _ in range(shard_count)]
        for r in records:
            key = f"{r.get('type')}:{r.get('id')}".encode("utf-8")
            buckets[int(hashlib.md5(key).hexdigest(), 16) % shard_count].append(r)
        for i, chunk in enumerate(buckets):
            if not chunk:
                continue
            lines = "\n".join(
                json.dumps(compact(r), ensure_ascii=False, separators=(",", ":"))
                for r in chunk
            )
            data = gzip.compress((lines + "\n").encode("utf-8"), compresslevel=9)
            digest = hashlib.md5(data).hexdigest()[:8]
            name = f"shards/{i:02d}-{digest}.jsonl.gz"
            (shard_dir / f"{i:02d}-{digest}.jsonl.gz").write_bytes(data)
            shards.append({"url": name, "n": len(chunk), "bytes": len(data)})
    else:
        # 传统有序分片（--shard-size 指定时保留）
        records.sort(key=lambda r: (-int(r.get("pubdate") or 0), r.get("title") or ""))
        for i in range(0, len(records), shard_size):
            chunk = records[i:i + shard_size]
            lines = "\n".join(
                json.dumps(compact(r), ensure_ascii=False, separators=(",", ":"))
                for r in chunk
            )
            data = gzip.compress((lines + "\n").encode("utf-8"), compresslevel=9)
            name = f"shards/{i // shard_size:04d}.jsonl.gz"
            (shard_dir / f"{i // shard_size:04d}.jsonl.gz").write_bytes(data)
            shards.append({"url": name, "n": len(chunk), "bytes": len(data)})
    return shards


def main():
    global args_desc_len
    parser = argparse.ArgumentParser(description="构建 BiliSearch 离线索引")
    parser.add_argument("--raw", default="data/raw", help="原始 JSONL 目录")
    parser.add_argument("--out", default="site/data", help="输出目录")
    parser.add_argument("--shard-size", type=int, default=3000,
                        help="每个分片多少条记录（默认 3000）")
    parser.add_argument("--shard-count", type=int, default=16,
                        help="稳定分桶数（默认 16；设 0 则用 --shard-size 有序分片）")
    parser.add_argument("--desc-len", type=int, default=180,
                        help="描述截断长度，控制索引体积")
    args = parser.parse_args()
    args_desc_len = args.desc_len

    raw_dir = Path(args.raw)
    out_dir = Path(args.out)
    records = load_records(raw_dir)
    shards = write_shards(records, out_dir, args.shard_size, args.shard_count)

    counts = {}
    for r in records:
        counts[r["type"]] = counts.get(r["type"], 0) + 1

    last_run = ""
    state_path = raw_dir.parent / "state.json"
    if state_path.exists():
        try:
            last_run = json.loads(state_path.read_text("utf-8")).get("last_run", "")
        except Exception:
            pass

    meta = {
        "v": 2,
        "types": TYPE_CODE,
        "built": time.strftime("%Y-%m-%d %H:%M:%S"),
        "updated": last_run,
        "total": len(records),
        "counts": counts,
        "typeNames": TYPE_NAMES,
        "shards": shards,
        "note": "shards[].url 可指向其他分支/仓库，实现多仓分片扩容",
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, separators=(",", ":")), "utf-8"
    )

    print(f"索引完成：{len(records)} 条 -> {len(shards)} 个分片，共 "
          f"{sum(s['bytes'] for s in shards) / 1024:.0f} KB（gzip）")
    print("各类型数量:", {TYPE_NAMES.get(k, k): v for k, v in counts.items()})


if __name__ == "__main__":
    sys.exit(main())
