#!/usr/bin/env python3
"""把 data/raw/*.jsonl 构建成静态站可用的紧凑 gzip 分片索引。

两种模式：
  compact —— 旧内存模式（小规模）：稳定分桶 + 内容哈希文件名
  routing —— 两级静态搜索（大规模）：数据分片 + 词->分片目录，按需下载

优化点（2026-09）：
  * 并行 worker 处理分片（词元化 + 压缩），默认按 CPU 数自动
  * gzip 压缩级别默认 6（比 9 快 2-3 倍，体积仅大 2-4%）
  * CJK bigram 提取用正则批量处理，替代逐字符循环
  * sqlite 关闭日志/同步并放大缓存，目录导出更快
  * 分阶段进度输出，长构建可见进度
"""

import argparse
import gc
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

try:
    from concurrent.futures import ProcessPoolExecutor
except Exception:  # pragma: no cover
    ProcessPoolExecutor = None

RAW_FILES = ["videos.jsonl", "users.jsonl", "dynamics.jsonl", "articles.jsonl"]
TYPE_NAMES = {"video": "视频", "user": "UP主", "dynamic": "动态", "article": "专栏"}
TYPE_CODE = {"video": 0, "user": 1, "dynamic": 2, "article": 3}

args_desc_len = 180  # 会被 main 覆盖

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9._#+\-]*")
_NON_CJK_RE = re.compile(r"[^\u3400-\u9fff]")


def log(msg):
    print(f"[build] {time.strftime('%H:%M:%S')} {msg}", flush=True)


def load_records(raw_dir: Path):
    """按 (type, id) 去重，后写入的覆盖先写入的（保留最新记录）。"""
    records = {}
    for fname in RAW_FILES:
        p = raw_dir / fname
        if not p.exists():
            continue
        t0 = time.time()
        n = 0
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                n += 1
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
        log(f"读取 {fname}: {n} 行 -> 累计去重 {len(records)} 条 ({time.time() - t0:.0f}s)")
    return list(records.values())


def _compact(rec, desc_len):
    out = {"i": rec.get("id", ""), "t": TYPE_CODE.get(rec.get("type"), 0)}
    out["s"] = rec.get("title", "")
    if rec.get("author"):
        out["a"] = rec["author"]
    if rec.get("author_id"):
        out["u"] = rec["author_id"]
    d = (rec.get("desc") or "")[:desc_len]
    if d:
        out["d"] = d
    p = int(rec.get("pubdate") or 0)
    if p:
        out["p"] = p
    if rec.get("category"):
        out["c"] = rec["category"]
    return out


def compact(rec):
    return _compact(rec, args_desc_len)


def py_tokenize(text):
    """与 site/search.js 的 tokenize 保持一致的词元化（拉丁词 + CJK bigram）。"""
    s = str(text or "").lower()
    tokens = _WORD_RE.findall(s)
    cjk = _NON_CJK_RE.sub("", s)
    if cjk:
        if len(cjk) == 1:
            tokens.append(cjk)
        else:
            tokens.extend(cjk[i:i + 2] for i in range(len(cjk) - 1))
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


# ---------------- compact 模式（小规模，内存索引） ----------------

def write_shards(records, out_dir: Path, shard_size: int, shard_count: int):
    shard_dir = out_dir / "shards"
    if shard_dir.exists():
        for old in shard_dir.rglob("*.gz"):
            old.unlink()
    shard_dir.mkdir(parents=True, exist_ok=True)
    shards = []
    if shard_count:
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
            data = gzip.compress((lines + "\n").encode("utf-8"), compresslevel=6)
            digest = hashlib.md5(data).hexdigest()[:8]
            (shard_dir / f"{i:02d}-{digest}.jsonl.gz").write_bytes(data)
            shards.append({"url": f"shards/{i:02d}-{digest}.jsonl.gz",
                           "n": len(chunk), "bytes": len(data)})
    else:
        records.sort(key=lambda r: (-int(r.get("pubdate") or 0), r.get("title") or ""))
        for i in range(0, len(records), shard_size):
            chunk = records[i:i + shard_size]
            lines = "\n".join(
                json.dumps(compact(r), ensure_ascii=False, separators=(",", ":"))
                for r in chunk
            )
            data = gzip.compress((lines + "\n").encode("utf-8"), compresslevel=6)
            (shard_dir / f"{i // shard_size:04d}.jsonl.gz").write_bytes(data)
            shards.append({"url": f"shards/{i // shard_size:04d}.jsonl.gz",
                           "n": len(chunk), "bytes": len(data)})
    return shards


# ---------------- routing 模式（大规模，两级静态搜索） ----------------

_W_CTX = {}


def _tmp_post_dir(out_dir: Path) -> Path:
    d = out_dir / ".tmp_post"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _worker_init(out_dir_str, desc_len, level, groups):
    out_dir = Path(out_dir_str)
    db = sqlite3.connect(str(_tmp_post_dir(out_dir) / f"post_{os.getpid()}.db"))
    db.execute("PRAGMA journal_mode=OFF")
    db.execute("PRAGMA synchronous=OFF")
    db.execute("PRAGMA temp_store=MEMORY")
    db.execute("CREATE TABLE IF NOT EXISTS post (token TEXT, shard INT, cnt INT)")
    _W_CTX.update(out_dir=out_dir, desc_len=desc_len, level=level, groups=groups, db=db)


def _process_shard_batch(batch):
    """worker：处理若干数据分片——写分片文件，并把词表写入本地 postings 库。"""
    out_dir = _W_CTX["out_dir"]
    desc_len = _W_CTX["desc_len"]
    level = _W_CTX["level"]
    groups = _W_CTX["groups"]
    db = _W_CTX["db"]
    entries = []
    for shard_id, items in batch:
        lines = []
        counts = {}
        for _key, rec in items:
            lines.append(json.dumps(_compact(rec, desc_len), ensure_ascii=False,
                                    separators=(",", ":")))
            for field in (rec.get("title"), rec.get("author"), rec.get("category"),
                          rec.get("desc"), rec.get("id")):
                seen = set()
                for tok in py_tokenize(field):
                    if tok in seen:
                        continue
                    seen.add(tok)
                    counts[tok] = counts.get(tok, 0) + 1
        data = gzip.compress(("\n".join(lines) + "\n").encode("utf-8"),
                             compresslevel=level)
        digest = hashlib.md5(data).hexdigest()[:8]
        group = shard_id % groups
        rel = f"g{group}/{shard_id:04d}-{digest}.gz"
        (out_dir / "shards" / rel).write_bytes(data)
        db.executemany("INSERT INTO post VALUES (?,?,?)",
                       ((t, shard_id, c) for t, c in counts.items()))
        entries.append({"id": shard_id, "group": group, "url": "shards/" + rel,
                        "n": len(items), "bytes": len(data)})
    db.commit()
    return entries


def build_routing(records, out_dir: Path, args):
    shard_dir = out_dir / "shards"
    dir_dir = out_dir / "dir"
    tmp_post = out_dir / ".tmp_post"
    for d in (shard_dir, dir_dir):
        if d.exists():
            for old in d.rglob("*.gz"):
                old.unlink()
    if tmp_post.exists():
        for old in tmp_post.glob("*.db"):
            old.unlink()
    shard_dir.mkdir(parents=True, exist_ok=True)
    dir_dir.mkdir(parents=True, exist_ok=True)
    tmp_post.mkdir(parents=True, exist_ok=True)

    total = len(records)
    shard_count = args.shard_count
    if shard_count <= 0:
        recs_per = max(1, args.recs_per_shard)
        shard_count = 2 ** math.ceil(math.log2(max(64, total / recs_per)))
        shard_count = min(8192, shard_count)
    groups = max(1, args.groups)

    t0 = time.time()
    buckets = [[] for _ in range(shard_count)]
    for r in records:
        key = f"{r.get('type')}:{r.get('id')}"
        idx = int(hashlib.md5(key.encode("utf-8")).hexdigest(), 16) % shard_count
        buckets[idx].append((key, r))
    log(f"分桶完成：{total} 条 -> {shard_count} 个数据分片 / {groups} 组 ({time.time() - t0:.0f}s)")
    for g in range(groups):
        (shard_dir / f"g{g}").mkdir(exist_ok=True)

    per_job = max(1, int(args.shards_per_job))
    jobs, batch = [], []
    for i, chunk in enumerate(buckets):
        if not chunk:
            continue
        batch.append((i, chunk))
        if len(batch) >= per_job:
            jobs.append(batch)
            batch = []
    if batch:
        jobs.append(batch)
    del buckets

    workers = max(1, int(args.workers))
    log(f"开始写分片：{workers} worker / {len(jobs)} 批（每批 {per_job} 分片）")
    shards = []
    done = 0

    def report(entries):
        nonlocal done
        done += len(entries)
        if args.progress_every and done % args.progress_every < len(entries):
            el = time.time() - t0
            log(f"分片 {done}/{shard_count} | {el:.0f}s | 预计剩余 {el / done * (shard_count - done):.0f}s")

    if workers > 1 and ProcessPoolExecutor is not None:
        with ProcessPoolExecutor(max_workers=workers, initializer=_worker_init,
                                 initargs=(str(out_dir), args.desc_len,
                                           args.compress_level, groups)) as ex:
            for entries in ex.map(_process_shard_batch, jobs, chunksize=1):
                shards.extend(entries)
                report(entries)
    else:
        _worker_init(str(out_dir), args.desc_len, args.compress_level, groups)
        for b in jobs:
            entries = _process_shard_batch(b)
            shards.extend(entries)
            report(entries)
        _W_CTX["db"].close()
    shards.sort(key=lambda s: s["id"])
    log(f"数据分片写完：{len(shards)} 个，用时 {time.time() - t0:.0f}s")

    # 合并各 worker 的 postings，建索引后导出目录
    t1 = time.time()
    del records                    # 分片已写完，及时释放原始记录，避免内存峰值
    gc.collect()
    merge_db = tmp_post / "merged.db"
    if merge_db.exists():
        merge_db.unlink()
    db = sqlite3.connect(str(merge_db))     # 落盘：2 亿级 词-分片 对不再吃内存
    db.isolation_level = None      # 自动提交：否则 DETACH 会报 database is locked
    db.execute("PRAGMA journal_mode=OFF")
    db.execute("PRAGMA synchronous=OFF")
    db.execute("PRAGMA temp_store=FILE")
    db.execute("PRAGMA cache_size=-200000")
    db.execute("CREATE TABLE post (token TEXT, shard INT, cnt INT)")
    files = sorted(tmp_post.glob("post_*.db"))
    merged = 0
    for i in range(0, len(files), 8):          # 每批最多挂 8 个库（SQLITE_MAX_ATTACHED=10）
        chunk = files[i:i + 8]
        aliases = []
        for n, dbfile in enumerate(chunk):
            alias = f"w{n}"
            db.execute(f"ATTACH DATABASE ? AS {alias}", (str(dbfile),))
            aliases.append(alias)
        for alias in aliases:
            db.execute(f"INSERT INTO post SELECT token, shard, cnt FROM {alias}.post")
        db.commit()
        for alias in aliases:
            db.execute(f"DETACH DATABASE {alias}")
        db.commit()
        merged += len(chunk)
    log(f"合并 {merged} 个 worker 词表 ({time.time() - t1:.0f}s)")
    if merged == 0:
        raise RuntimeError("worker 词表合并失败（0 个库），已中止以避免发布空目录索引")
    n_rows = db.execute("SELECT COUNT(*) FROM post").fetchone()[0]
    log(f"词条-分片对共 {n_rows} 条")

    t2 = time.time()
    db.execute("CREATE INDEX post_idx ON post(token, cnt DESC, shard)")
    log(f"目录索引建立完成 ({time.time() - t2:.0f}s)")

    dir_shards = []
    buf, buf_size, cur_min, cur_max, cur_token, pairs = [], 0, None, None, None, []
    idx = 0

    def flush_dir():
        nonlocal buf, buf_size, cur_min, cur_max, idx
        if not buf:
            return
        data = gzip.compress(("\n".join(buf) + "\n").encode("utf-8"),
                             compresslevel=args.compress_level)
        digest = hashlib.md5(data).hexdigest()[:8]
        name = f"{idx:03d}-{digest}.gz"
        (dir_dir / name).write_bytes(data)
        dir_shards.append({"id": idx, "url": "dir/" + name, "min": cur_min,
                           "max": cur_max, "n": len(buf), "bytes": len(data)})
        idx += 1
        buf, buf_size, cur_min, cur_max = [], 0, None, None

    n_tokens = 0
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
            n_tokens += 1
            if cur_min is None:
                cur_min = token
            cur_max = token
        pairs.append(f"{cnt}:{shard}")
    if cur_token is not None:
        buf.append(f"{cur_token}\t{','.join(pairs)}")
    flush_dir()
    db.close()
    for old in tmp_post.glob("*.db"):
        try:
            old.unlink()
        except Exception:
            pass
    log(f"目录写完：{len(dir_shards)} 个目录分片 / {n_tokens} 个词，用时 {time.time() - t1:.0f}s")
    if not dir_shards:
        raise RuntimeError("目录分片为空，已中止以避免发布不可搜索的索引")

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
                        help="compact 模式：每个分片多少条记录")
    parser.add_argument("--shard-count", type=int, default=0,
                        help="0=自动；routing 按 --recs-per-shard 自动定分片数")
    parser.add_argument("--recs-per-shard", type=int, default=3000,
                        help="routing 模式：每个数据分片目标条数")
    parser.add_argument("--groups", type=int, default=1,
                        help="routing 模式：分片分组数（多仓库/分支托管）")
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
    parser.add_argument("--workers", type=int, default=0,
                        help="并行 worker 数（0=按 CPU 自动，1=单进程）")
    parser.add_argument("--compress-level", type=int, default=6,
                        help="gzip 压缩级别（默认 6，比 9 快 2-3 倍）")
    parser.add_argument("--progress-every", type=int, default=200,
                        help="每处理多少个分片打印一次进度（0=关闭）")
    parser.add_argument("--shards-per-job", type=int, default=8,
                        help="每个并行任务处理多少个分片（默认 8）")
    args = parser.parse_args()
    args_desc_len = args.desc_len
    if args.workers <= 0:
        args.workers = min(8, max(1, (os.cpu_count() or 4)))

    lock = acquire_build_lock(Path(args.out) / ".build.lock")
    if lock is None:
        print(f"[lock] 另一个 build_index.py 正在运行（{args.out}/.build.lock 被占用），"
              f"为避免互相覆盖已退出", file=sys.stderr)
        return 1

    raw_dir = Path(args.raw)
    out_dir = Path(args.out)
    t_start = time.time()
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
        log(f"模式={mode} 记录数={len(records)} workers={args.workers} 压缩级别={args.compress_level}")

        dir_shards = []
        if mode == "routing":
            shards, dir_shards, shard_count, groups = build_routing(records, out_dir, args)
            meta = {
                "v": 3,
                "type": "routing",
                "built": time.strftime("%Y-%m-%d %H:%M:%S"),
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
            json.dumps(meta, ensure_ascii=False, separators=(",", ":")), "utf-8")

        dir_count = len(dir_shards) if mode == "routing" else 0
        total_bytes = sum(s["bytes"] for s in shards)
        print(f"索引完成：{len(records)} 条 -> {len(shards)} 个数据分片"
              + (f" + {dir_count} 个目录分片" if dir_count else "")
              + f"，共 {total_bytes / 1024:.0f} KB（gzip），总用时 {time.time() - t_start:.0f}s")
        print("各类型数量:", {TYPE_NAMES.get(k, k): v for k, v in counts.items()})
        return 0
    finally:
        if lock:
            try:
                lock.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
