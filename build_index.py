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
import math
import os
import re
import sqlite3
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


def py_tokenize(text):
    """与 site/search.js 的 tokenize 保持一致的词元化（bigram + 拉丁词）。"""
    s = str(text or "").lower()
    tokens = re.findall(r"[a-z0-9][a-z0-9._#+\-]*", s)
    cjk = [ch for ch in s if "\u3400" <= ch <= "\u9fff"]
    for i in range(len(cjk) - 1):
        tokens.append(cjk[i] + cjk[i + 1])
    if len(cjk) == 1:
        tokens.append(cjk[0])
    return tokens


def acquire_build_lock(lock_path):
    """构建互斥锁：防止两个 build_index.py 并发（会互相清掉对方的分片文件）。"""
    lock_path = Path(lock_path).resolve()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        f = lock_path.open("r+", encoding="utf-8")
        created = False
    except FileNotFoundError:
        try:
            f = lock_path.open("x", encoding="utf-8")
            created = True
        except FileExistsError:
            f = lock_path.open("r+", encoding="utf-8")
            created = False
    try:
        f.seek(0)
        if created or not f.read(1):
            f.write("0")
            f.flush()
        f.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            f.close()
        except Exception:
            pass
        return None
    f.seek(0)
    f.write(str(os.getpid()))
    f.truncate()
    f.flush()
    return f


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


def build_routing(records, out_dir: Path, args):
    """路由模式：数据分片 + 词->分片目录（两级静态搜索，客户端按需下载）。"""
    shard_dir = out_dir / "shards"
    dir_dir = out_dir / "dir"
    for d in (shard_dir, dir_dir):
        if d.exists():
            for old in d.glob("**/*.gz"):
                old.unlink()
    shard_dir.mkdir(parents=True, exist_ok=True)
    dir_dir.mkdir(parents=True, exist_ok=True)

    total = len(records)
    shard_count = args.shard_count
    if shard_count <= 0:
        recs_per = max(1, args.recs_per_shard)
        shard_count = 2 ** math.ceil(math.log2(max(64, total / recs_per)))
        shard_count = min(8192, shard_count)
    groups = max(1, args.groups)

    buckets = [[] for _ in range(shard_count)]
    for r in records:
        key = f"{r.get('type')}:{r.get('id')}".encode("utf-8")
        buckets[int(hashlib.md5(key).hexdigest(), 16) % shard_count].append(r)

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE post (token TEXT, shard INT, cnt INT, PRIMARY KEY(token, shard))")

    shards = []
    for i, chunk in enumerate(buckets):
        if not chunk:
            continue
        lines = "\n".join(
            json.dumps(compact(r), ensure_ascii=False, separators=(",", ":"))
            for r in chunk
        )
        data = gzip.compress((lines + "\n").encode("utf-8"), compresslevel=9)
        group = i % groups
        (shard_dir / f"g{group}").mkdir(exist_ok=True)
        digest = hashlib.md5(data).hexdigest()[:8]
        name = f"g{group}/{i:04d}-{digest}.gz"
        (shard_dir / name).write_bytes(data)
        shards.append({"id": i, "group": group, "url": "shards/" + name,
                       "n": len(chunk), "bytes": len(data)})
        counts = {}
        for r in chunk:
            for field in (r.get("title"), r.get("author"), r.get("category"), r.get("desc"), r.get("id")):
                seen = set()
                for tok in py_tokenize(field):
                    if tok in seen:
                        continue
                    seen.add(tok)
                    counts[tok] = counts.get(tok, 0) + 1
        db.executemany("INSERT OR REPLACE INTO post VALUES (?,?,?)",
                       [(t, i, c) for t, c in counts.items()])
    db.commit()

    # 导出目录：按词排序、切成若干 dir 分片（词-> "cnt:shard,..." 按 cnt 降序）
    dir_shards = []
    buf, buf_size, cur_min, cur_max, cur_token, pairs = [], 0, None, None, None, []
    idx = 0

    def flush_dir():
        nonlocal buf, buf_size, cur_min, cur_max, idx
        if not buf:
            return
        text = "\n".join(buf) + "\n"
        data = gzip.compress(text.encode("utf-8"), compresslevel=9)
        digest = hashlib.md5(data).hexdigest()[:8]
        name = f"{idx:03d}-{digest}.gz"
        (dir_dir / name).write_bytes(data)
        dir_shards.append({"id": idx, "url": "dir/" + name, "min": cur_min,
                           "max": cur_max, "n": len(buf), "bytes": len(data)})
        idx += 1
        buf, buf_size, cur_min, cur_max = [], 0, None, None

    for token, shard, cnt in db.execute(
            "SELECT token, shard, cnt FROM post ORDER BY token, cnt DESC, shard"):
        if token != cur_token:
            if cur_token is not None:
                line = f"{cur_token}\t{','.join(pairs)}"
                buf.append(line)
                buf_size += len(line) + 1
                if buf_size >= args.dir_target:
                    flush_dir()
            cur_token, pairs = token, []
            if cur_min is None:
                cur_min = token
            cur_max = token
        pairs.append(f"{cnt}:{shard}")
    if cur_token is not None:
        line = f"{cur_token}\t{','.join(pairs)}"
        buf.append(line)
        buf_size += len(line) + 1
    flush_dir()

    # 多仓库：按 group 把分片 URL 换成外部 base（jsdelivr/raw 等）
    if args.shard_bases:
        cfg = json.loads(Path(args.shard_bases).read_text("utf-8"))
        bases = {int(b["group"]): b["url"] for b in cfg.get("bases", [])}
        for sh in shards:
            base = bases.get(sh["group"])
            if base:
                sh["url"] = base.rstrip("/") + "/" + sh["url"]
    return shards, dir_shards, shard_count, groups


def main():
    global args_desc_len
    parser = argparse.ArgumentParser(description="构建 BiliSearch 离线索引")
    parser.add_argument("--raw", default="data/raw", help="原始 JSONL 目录")
    parser.add_argument("--out", default="site/data", help="输出目录")
    parser.add_argument("--mode", choices=["auto", "compact", "routing"], default="auto",
                        help="auto=超 15 万条自动用路由模式；compact=旧内存模式；routing=两级静态搜索")
    parser.add_argument("--shard-size", type=int, default=3000,
                        help="每个分片多少条记录（默认 3000）")
    parser.add_argument("--shard-count", type=int, default=0,
                        help="0=自动（compact 用 16 桶稳定分片，routing 按 --recs-per-shard 定）；显式指定则固定")
    parser.add_argument("--recs-per-shard", type=int, default=1500,
                        help="routing 模式：每个数据分片目标条数（自动定分片数）")
    parser.add_argument("--groups", type=int, default=1,
                        help="routing 模式：分片分组数（用于多仓库/分支托管）")
    parser.add_argument("--dir-target", type=int, default=1500000,
                        help="routing 模式：每个目录分片未压缩字节预算")
    parser.add_argument("--search-budget", type=int, default=24000000,
                        help="routing 模式：单次查询最多下载多少字节数据分片")
    parser.add_argument("--search-max-shards", type=int, default=64,
                        help="routing 模式：单次查询最多下载多少个数据分片")
    parser.add_argument("--shard-bases", default="",
                        help="routing 模式：多仓库配置 JSON（bases: [{group, url}]）")
    parser.add_argument("--desc-len", type=int, default=180,
                        help="描述截断长度，控制索引体积")
    args = parser.parse_args()
    args_desc_len = args.desc_len

    lock = acquire_build_lock(Path(args.out) / ".build.lock")
    if lock is None:
        print(f"[lock] 另一个 build_index.py 正在运行（{args.out}/.build.lock 被占用），"
              f"为避免互相覆盖已退出", file=sys.stderr)
        return 1

    raw_dir = Path(args.raw)
    out_dir = Path(args.out)
    try:
        records = load_records(raw_dir)

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

        mode = args.mode
        if mode == "auto":
            mode = "routing" if len(records) > 150000 else "compact"
        built = time.strftime("%Y-%m-%d %H:%M:%S")
        if mode == "routing":
            shards, dir_shards, shard_count, groups = build_routing(records, out_dir, args)
            meta = {
                "v": 3,
                "type": "routing",
                "built": built,
                "updated": last_run,
                "total": len(records),
                "counts": counts,
                "typeNames": TYPE_NAMES,
                "groups": groups,
                "shardCount": shard_count,
                "shards": shards,
                "dirShards": dir_shards,
                "search": {"budgetBytes": args.search_budget,
                           "maxShards": args.search_max_shards},
                "note": "路由模式：查询时按目录只下载相关分片，不加载全量",
            }
        else:
            shards = write_shards(records, out_dir, args.shard_size, args.shard_count or 16)
            meta = {
                "v": 2,
                "types": TYPE_CODE,
                "built": built,
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

        print(f"索引完成：{len(records)} 条 -> {len(shards)} 个数据分片"
              + (f" + {len(dir_shards)} 个目录分片" if mode == "routing" else "")
              + f"，共 {sum(s['bytes'] for s in shards) / 1024:.0f} KB（gzip）")
        print("各类型数量:", {TYPE_NAMES.get(k, k): v for k, v in counts.items()})
    finally:
        if lock:
            try:
                lock.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
