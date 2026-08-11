"""图式爬虫：从种子（视频/用户/动态/专栏 ID）出发，沿关系边扩展抓取元数据。

关系边：
    视频 -> 作者（用户）、相关推荐视频
    用户 -> 投稿视频、动态、专栏
    动态 -> 引用的视频/专栏、作者
    专栏 -> 作者

产物：data/raw/{videos,users,dynamics,articles}.jsonl（追加式），
     data/state.json（cookie/WBI 密钥/去重状态）。
"""

import argparse
import json
import math
import os
import random
import re
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from queue import Empty, Queue

from .bili import BiliClient, BiliError, av2bv

RAW_FILES = {
    "video": "videos.jsonl",
    "user": "users.jsonl",
    "dynamic": "dynamics.jsonl",
    "article": "articles.jsonl",
}


class ProgressReporter(threading.Thread):
    """每 interval 秒输出一次整体进度；TTY 下用 \r 原地刷新。"""

    def __init__(self, stop_event, lock, stats, total=None, interval=5.0, label=""):
        super().__init__(daemon=True)
        self.stop_event = stop_event
        self.lock = lock
        self.stats = stats
        self.total = total
        self.interval = interval
        self.label = label
        self.started = time.time()

    def run(self):
        tty = sys.stdout.isatty()
        while not self.stop_event.wait(self.interval):
            elapsed = time.time() - self.started
            with self.lock:
                ok = self.stats.get("ok", 0)
                err = self.stats.get("err", 0)
            done = ok + err
            rate = done / elapsed * 60 if elapsed > 1 else 0.0
            total = self.total if (self.total is not None and self.total < 10 ** 8) else "?"
            line = (f"[进度 {self.label}] {elapsed:.0f}s | {done}/{total} | "
                    f"成功 {ok} 失败 {err} | {rate:.0f} 条/分")
            if tty:
                sys.stdout.write("\r" + line + " " * 8)
            else:
                sys.stdout.write(line + "\n")
            sys.stdout.flush()
        if tty:
            sys.stdout.write("\r" + " " * 80 + "\r")
            sys.stdout.flush()


def install_sigint(stop_event):
    """第一次 Ctrl+C 优雅停止（当前请求结束后退出）；第二次强制退出。"""
    import signal

    def handler(signum, frame):
        if getattr(handler, "pressed", False):
            print("\n[强制退出]", file=sys.stderr)
            os._exit(130)
        handler.pressed = True
        stop_event.set()
        print("\n[Ctrl+C] 正在停止（当前请求结束后退出），再按一次强制退出…", file=sys.stderr)
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, handler)


def acquire_lock(lock_path):
    """进程级互斥锁：防止误开多个爬取进程（同文件同时跑会重复抓取并互相覆盖状态）。
    返回锁文件句柄；已被占用时返回 None。"""
    lock_path = Path(lock_path).resolve()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # r+：不截断；不存在时用 "x"（O_EXCL）原子创建，避免与竞争者互相截断
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
            f.write("0")  # 保证至少 1 字节可锁
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


def stop_requested(stop_file):
    """停止标记文件存在即请求停止（跨平台、不依赖 Ctrl+C 能否送达）。"""
    return bool(stop_file) and Path(stop_file).exists()


def _save_state(state, state_path):
    """原子写 state.json；失败只警告不中断。"""
    try:
        tmp = str(state_path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, state_path)
        return True
    except Exception as e:
        print(f"[warn] 状态保存失败: {e}", file=sys.stderr)
        return False

VIDEO_RE = re.compile(r"(BV[0-9A-Za-z]{10}|av\d+)", re.I)
MID_RE = re.compile(r"(?:mid|uid)\s*[:：]?\s*(\d+)", re.I)
CV_RE = re.compile(r"(?:cv|read[/_]cv|article[/?]id[=:])(\d+)", re.I)
DYN_RE = re.compile(r"(?:t\.bilibili\.com[/_])(\d+)", re.I)
SPACE_RE = re.compile(r"space\.bilibili\.com/(\d+)")
REL_TIME_RE = re.compile(r"(\d+)\s*(分钟|小时|天|周|月|年)?前")


def parse_seed(text):
    """把一行种子解析成 (type, id)；无法识别返回 None。"""
    s = text.strip()
    if not s or s.startswith("#"):
        return None
    m = DYN_RE.search(s)
    if m:
        return ("dynamic", m.group(1))
    if SPACE_RE.search(s):
        return ("user", SPACE_RE.search(s).group(1))
    m = CV_RE.search(s)
    if m and ("read" in s or "cv" in s.lower()):
        return ("article", m.group(1))
    m = VIDEO_RE.search(s)
    if m:
        return ("video", m.group(1))
    m = MID_RE.search(s)
    if m:
        return ("user", m.group(1))
    m = CV_RE.search(s)
    if m:
        return ("article", m.group(1))
    return None


def _clean(s, n=None):
    if s is None:
        return ""
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s if n is None else s[:n]


def _rec(typ, ident, title, author, author_id, desc, url, pubdate=0,
         category="", **extra):
    return {
        "id": ident,
        "type": typ,
        "title": _clean(title, 300),
        "author": _clean(author, 120),
        "author_id": str(author_id or ""),
        "desc": _clean(desc, 500),
        "url": url,
        "pubdate": int(pubdate or 0),
        "category": _clean(category, 60),
        **extra,
    }


def parse_pub_time(pub_time):
    """把 module_author.pub_time 这类字符串转成 epoch 秒；解析失败返回 0。"""
    if not pub_time:
        return 0
    s = str(pub_time).strip()
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})", s)
    if m:
        y, mo, d, h, mi = map(int, m.groups())
        return int(time.mktime((y, mo, d, h, mi, 0, 0, 0, -1)))
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        y, mo, d = map(int, m.groups())
        return int(time.mktime((y, mo, d, 0, 0, 0, 0, 0, -1)))
    if "刚刚" in s:
        return int(time.time())
    m = REL_TIME_RE.search(s)
    if m:
        unit = m.group(2) or "分钟"
        mult = {"分钟": 60, "小时": 3600, "天": 86400, "周": 604800,
                "月": 2592000, "年": 31536000}[unit]
        return int(time.time()) - int(m.group(1)) * mult
    return 0


def _owner_of(d):
    o = d.get("owner") or {}
    if isinstance(o, dict):
        return _clean(o.get("name")), o.get("mid")
    if isinstance(d.get("author"), dict):
        a = d["author"]
        return _clean(a.get("name") or a.get("author_name")), a.get("mid")
    return _clean(d.get("author_name") or d.get("name")), d.get("mid")


def dynamic_record(item):
    """把动态 feed item 转成索引记录 + 邻居。"""
    mod = item.get("modules") or {}
    auth = mod.get("module_author") or {}
    dyn = mod.get("module_dynamic") or {}
    major = dyn.get("major") or {}
    desc = _clean((dyn.get("desc") or {}).get("text") or "")
    id_str = str(item.get("id_str") or "")
    mtype = major.get("type")
    title, pub, neigh = "", 0, []
    if mtype == "MAJOR_TYPE_ARCHIVE":
        a = major.get("archive") or {}
        title = _clean(a.get("title"))
        pub = a.get("pub_date") or a.get("pub_ts") or 0
        if a.get("bvid"):
            neigh.append(("video", a["bvid"]))
    elif mtype == "MAJOR_TYPE_ARTICLE":
        a = major.get("article") or {}
        title = _clean(a.get("title"))
        if a.get("cvid"):
            neigh.append(("article", "cv" + str(a["cvid"])))
    elif mtype == "MAJOR_TYPE_DRAW":
        items = major.get("items") or []
        title = _clean((items[0] or {}).get("title")) if items else ""
    elif mtype == "MAJOR_TYPE_OPUS":
        title = _clean(((major.get("opus") or {}).get("summary") or {}).get("text"), 80)
    if not title:
        title = next((ln.strip() for ln in desc.split("\n") if ln.strip()), "")[:60]
    if not title:
        title = "转发动态" if "FORWARD" in str(item.get("type") or "") else "动态"
    if not pub:
        pub = parse_pub_time(auth.get("pub_time"))
    name, mid = _clean(auth.get("name")), auth.get("mid")
    if mid:
        neigh.append(("user", str(mid)))
    rec = _rec("dynamic", "dyn:" + id_str, title, name, mid, desc,
               f"https://t.bilibili.com/{id_str}", pub,
               category=str(item.get("type") or "").replace("DYNAMIC_TYPE_", ""))
    return rec, neigh


def derive_user_rec(vrec):
    """从视频记录派生 UP 主记录（零额外请求，作者名/ID 来自视频响应）。"""
    mid = vrec.get("author_id")
    name = vrec.get("author")
    if not mid or not name:
        return None
    return _rec("user", "mid:" + str(mid), name, name, mid, "",
                f"https://space.bilibili.com/{mid}", 0, "UP主")


def fetch_author_side(client, mid, args):
    """漫游用轻量作者扩展：只抓资料 + 少量专栏/动态，不抓其投稿视频。"""
    name, sign = "", ""
    try:
        d = client.user(mid)
        name, sign = d.get("name") or "", d.get("sign") or ""
    except BiliError:
        try:
            d = client.user_card(mid)
            name, sign = d.get("name") or "", d.get("sign") or ""
        except BiliError:
            pass
    if not name:
        name = f"UID {mid}"
    rec = _rec("user", "mid:" + str(mid), name, name, mid, sign,
               f"https://space.bilibili.com/{mid}", 0, "UP主")
    neigh = []
    try:
        arts, _ = client.user_articles(mid, 1, args.ps)
        for a in arts[:args.neighbor_articles]:
            if a.get("id"):
                neigh.append(("article", "cv" + str(a["id"])))
    except BiliError:
        pass
    try:
        items, has_more, offset = client.user_dynamics(mid)
        for it in items[:args.neighbor_dynamics]:
            nid = it.get("id_str")
            if nid:
                neigh.append(("dynamic", str(nid)))
    except BiliError:
        pass
    return rec, neigh


def fetch_item(client, typ, ident, args):
    """抓取一个实体，返回 (记录, 邻居列表)；失败抛 BiliError。"""
    if typ == "video":
        d = client.video(ident)
        name, mid = _owner_of(d)
        rec = _rec("video", ident, d.get("title"), name, mid, d.get("desc"),
                   f"https://www.bilibili.com/video/{ident}", d.get("pubdate"),
                   d.get("tname"), view=(d.get("stat") or {}).get("view"))
        neigh = [("user", str(mid))] if mid else []
        try:
            for v in client.video_related(ident, args.related):
                bv = v.get("bvid")
                if bv:
                    neigh.append(("video", bv))
        except BiliError:
            pass
        return rec, neigh

    if typ == "user":
        name, sign = "", ""
        try:
            d = client.user(ident)
            name, sign = d.get("name") or "", d.get("sign") or ""
        except BiliError:
            try:
                d = client.user_card(ident)
                name, sign = d.get("name") or "", d.get("sign") or ""
            except BiliError as e:
                print(f"  [warn] 用户 {ident} 资料获取失败，使用占位名: {e}")
        if not name:
            name = f"UID {ident}"
        rec = _rec("user", "mid:" + str(ident), name, name, ident, sign,
                   f"https://space.bilibili.com/{ident}", 0, category="UP主")
        neigh = []
        try:
            vlist, total = client.user_videos(ident, 1, args.ps)
            want = max(0, args.neighbor_videos - len([n for n in neigh if n[0] == "video"]))
            for v in vlist[:want]:
                if v.get("bvid"):
                    neigh.append(("video", v["bvid"]))
            pages = min(args.user_pages, math.ceil(total / args.ps))
            for pn in range(2, pages + 1):
                if len([n for n in neigh if n[0] == "video"]) >= args.neighbor_videos:
                    break
                vlist, _ = client.user_videos(ident, pn, args.ps)
                for v in vlist:
                    if len([n for n in neigh if n[0] == "video"]) >= args.neighbor_videos:
                        break
                    if v.get("bvid"):
                        neigh.append(("video", v["bvid"]))
        except BiliError as e:
            print(f"  [warn] 用户 {ident} 投稿拉取失败: {e}")
        try:
            items, has_more, offset = client.user_dynamics(ident)
            for it in items:
                if len([n for n in neigh if n[0] == "dynamic"]) >= args.neighbor_dynamics:
                    break
                nid = it.get("id_str")
                if nid:
                    neigh.append(("dynamic", str(nid)))
            pages = 0
            while (len([n for n in neigh if n[0] == "dynamic"]) < args.neighbor_dynamics
                   and has_more and pages < args.user_pages - 1):
                items, has_more, offset = client.user_dynamics(ident, offset)
                for it in items:
                    if len([n for n in neigh if n[0] == "dynamic"]) >= args.neighbor_dynamics:
                        break
                    nid = it.get("id_str")
                    if nid:
                        neigh.append(("dynamic", str(nid)))
                pages += 1
        except BiliError as e:
            print(f"  [warn] 用户 {ident} 动态拉取失败: {e}")
        try:
            arts, total = client.user_articles(ident, 1, args.ps)
            for a in arts:
                if len([n for n in neigh if n[0] == "article"]) >= args.neighbor_articles:
                    break
                if a.get("id"):
                    neigh.append(("article", "cv" + str(a["id"])))
            pages = min(args.user_pages, math.ceil(total / args.ps))
            for pn in range(2, pages + 1):
                if len([n for n in neigh if n[0] == "article"]) >= args.neighbor_articles:
                    break
                arts, _ = client.user_articles(ident, pn, args.ps)
                for a in arts:
                    if len([n for n in neigh if n[0] == "article"]) >= args.neighbor_articles:
                        break
                    if a.get("id"):
                        neigh.append(("article", "cv" + str(a["id"])))
        except BiliError as e:
            print(f"  [warn] 用户 {ident} 专栏拉取失败: {e}")
        return rec, neigh

    if typ == "dynamic":
        data = client.dynamic_detail(ident)
        item = (data.get("item") or {}) if isinstance(data, dict) else {}
        if not item:
            raise BiliError(f"动态 {ident} 无详情数据")
        return dynamic_record(item)

    if typ == "article":
        d = client.article(ident)
        name, mid = _owner_of(d)
        rec = _rec("article", "cv" + str(ident), d.get("title"), name, mid,
                   d.get("summary") or d.get("desc"), f"https://www.bilibili.com/read/cv{ident}",
                   d.get("publish_time") or d.get("ctime"), d.get("category"),
                   view=d.get("view"))
        neigh = [("user", str(mid))] if mid else []
        return rec, neigh

    raise BiliError(f"未知类型 {typ}")


def load_seeds(args):
    seeds = []
    if args.seeds:
        for line in Path(args.seeds).read_text("utf-8").splitlines():
            s = parse_seed(line)
            if s:
                seeds.append(s)
    for seed in args.seed or []:
        s = parse_seed(seed)
        if s:
            seeds.append(s)
    return seeds


def collect_bv_seeds(args, client):
    """批量收集视频 ID：--bv-file / --seed / --popular / --ranking。"""
    seeds = []
    if args.bv_file:
        for line in Path(args.bv_file).read_text("utf-8").splitlines():
            s = parse_seed(line)
            if s and s[0] == "video":
                seeds.append(s[1])
    for seed in args.seed or []:
        s = parse_seed(seed)
        if s and s[0] == "video":
            seeds.append(s[1])
    if args.popular > 0:
        pages = max(1, math.ceil(args.popular / 20))
        for pn in range(1, pages + 1):
            try:
                for v in client.popular(pn, 20):
                    if v.get("bvid"):
                        seeds.append(v["bvid"])
            except BiliError as e:
                print(f"  [warn] 热门第 {pn} 页失败: {e}")
    if args.ranking:
        for rid in args.ranking.split(","):
            rid = rid.strip()
            if not rid:
                continue
            try:
                for v in client.ranking(int(rid)):
                    if v.get("bvid"):
                        seeds.append(v["bvid"])
                print(f"  [ok] 排行榜 rid={rid} 收集完成")
            except BiliError as e:
                print(f"  [warn] 排行榜 rid={rid} 失败: {e}")
    seen = set()
    uniq = []
    for bv in seeds:
        if bv not in seen:
            seen.add(bv)
            uniq.append(bv)
    return uniq


def run_burst(args):
    """视频批量快抓：多线程只抓视频元数据，不展开作者/动态/专栏。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    args.related = 0  # burst 只抓视频本身；需要相关推荐扩展请用 crawl 模式
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = data_dir / "raw"
    raw_dir.mkdir(exist_ok=True)
    state_path = data_dir / "state.json"
    state = {}
    if state_path.exists():
        state = json.loads(state_path.read_text("utf-8"))
    if args.cookie and not state.get("cookie_extra"):
        state["cookie_extra"] = args.cookie
    if args.cookies_file and Path(args.cookies_file).exists() and not state.get("cookie_extra"):
        state["cookie_extra"] = Path(args.cookies_file).read_text("utf-8").strip()

    # 预热身份（cookie/WBI 密钥），避免多线程并发初始化竞态
    client = BiliClient(state, interval=args.interval)
    client._ensure_cookies()
    client._ensure_wbi()

    bvs = collect_bv_seeds(args, client)
    seen_state = state.setdefault("seen", {})
    now = int(time.time())
    refresh = args.refresh_days * 86400
    todo = [
        bv for bv in bvs
        if f"video:{bv}" not in seen_state
        or now - int(seen_state.get(f"video:{bv}", 0)) >= refresh
    ]
    print(f"收集到 {len(bvs)} 个视频 ID，去重/刷新后待抓 {len(todo)} 个；"
          f"workers={args.workers}，每 worker 间隔 {args.interval}s")
    if not todo:
        print("没有需要抓取的视频（都在 refresh-days 内）")
        return 0

    out_files = {}
    for typ, fname in RAW_FILES.items():
        out_files[typ] = (raw_dir / fname).open("a", encoding="utf-8")
    lock = threading.Lock()
    stats = {"ok": 0, "err": 0}
    derived_users = set()
    stop_event = threading.Event()
    install_sigint(stop_event)
    work_queue = Queue()
    for bv in todo:
        work_queue.put(bv)
    reporter = ProgressReporter(stop_event, lock, stats, total=len(todo), label="burst")
    reporter.start()

    def worker():
        c = BiliClient(state, interval=args.interval)
        while not stop_event.is_set():
            if stop_requested(args.stop_file):
                stop_event.set()
                break
            try:
                bv = work_queue.get_nowait()
            except Empty:
                return
            key = f"video:{bv}"
            try:
                rec, _ = fetch_item(c, "video", bv, args)
            except BiliError as e:
                with lock:
                    stats["err"] += 1
                print(f"[skip] {key}: {e}")
                continue
            if not rec:
                continue
            with lock:
                out_files["video"].write(json.dumps(rec, ensure_ascii=False) + "\n")
                out_files["video"].flush()
                seen_state[key] = now
                stats["ok"] += 1
                urec = derive_user_rec(rec)
                if urec and urec["id"] not in derived_users and urec["id"] not in seen_state:
                    derived_users.add(urec["id"])
                    out_files["user"].write(json.dumps(urec, ensure_ascii=False) + "\n")
                    out_files["user"].flush()
            print(f"[ok] {key} | {rec['title'][:48]}")

    threads = [threading.Thread(target=worker, daemon=True)
               for _ in range(max(1, args.workers))]
    for t in threads:
        t.start()
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        print("\n[已停止] 正在保存状态…", file=sys.stderr)
    finally:
        stop_event.set()
        reporter.join(timeout=1)
        for f in out_files.values():
            f.close()
        state["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _save_state(state, state_path)
    print(f"burst 结束：成功 {stats['ok']}，失败 {stats['err']}")
    return 0


class _JumpBag:
    """漫游模式的“随机跳转源”：跳出相关推荐的小圈子，覆盖全站。

    sources 支持：
      aid      —— 随机 av 号探测（按上传顺序近似均匀采样全站）
      precious —— 入站必刷（经典视频）
      series   —— 每周必看随机期数
      popular  —— 热门榜随机页
    """

    def __init__(self, args, rng, lock):
        self.args = args
        self.rng = rng
        self.lock = lock
        self.sources = [s.strip() for s in (args.jump_sources or "aid").split(",") if s.strip()]
        self.pools = {}
        self.series_cache = {}

    def _pool(self, c, name):
        with self.lock:
            if name in self.pools:
                return self.pools[name]
        items = []
        try:
            if name == "precious":
                items = [v.get("bvid") for v in c.precious()]
            elif name == "popular":
                for pn in (1, 2, 3):
                    items += [v.get("bvid") for v in c.popular(pn, 20)]
        except BiliError:
            pass
        items = [b for b in items if b]
        with self.lock:
            self.pools[name] = items
        return items

    def get(self, c):
        src = self.rng.choice(self.sources)
        if src == "aid":
            # 随机 av 探测：命中率取决于区间内现存稿件比例；最多试 3 次
            for _ in range(3):
                aid = self.rng.randint(self.args.aid_min, self.args.aid_max)
                try:
                    d = c.video(f"av{aid}")
                    return d.get("bvid") or av2bv(aid)
                except BiliError:
                    continue
            return None
        if src == "series":
            num = self.rng.randint(1, self.args.series_max)
            with self.lock:
                cached = self.series_cache.get(num)
            if cached is None:
                try:
                    items = [v.get("bvid") for v in c.series(num)]
                except BiliError:
                    items = []
                cached = [b for b in items if b]
                with self.lock:
                    self.series_cache[num] = cached
            return self.rng.choice(cached) if cached else None
        pool = self._pool(c, src)
        return self.rng.choice(pool) if pool else None


def run_roam(args):
    """全站漫游：相关推荐随机游走 + 随机跳转源，近似覆盖全站视频。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = data_dir / "raw"
    raw_dir.mkdir(exist_ok=True)
    state_path = data_dir / "state.json"
    state = {}
    if state_path.exists():
        state = json.loads(state_path.read_text("utf-8"))
    if args.cookie and not state.get("cookie_extra"):
        state["cookie_extra"] = args.cookie
    if args.cookies_file and Path(args.cookies_file).exists() and not state.get("cookie_extra"):
        state["cookie_extra"] = Path(args.cookies_file).read_text("utf-8").strip()

    client = BiliClient(state, interval=args.interval)
    client._ensure_cookies()
    client._ensure_wbi()

    seen_state = state.setdefault("seen", {})
    run_seen = set()
    now = int(time.time())
    refresh = args.refresh_days * 86400
    lock = threading.Lock()
    rng = random.Random()
    jump = _JumpBag(args, rng, lock)

    # 初始起点：种子/热门/排行榜等（与 burst 共用收集逻辑）
    start_pool = collect_bv_seeds(args, client)
    rng.shuffle(start_pool)

    out_files = {}
    for typ, fname in RAW_FILES.items():
        out_files[typ] = (raw_dir / fname).open("a", encoding="utf-8")
    stats = {"ok": 0, "err": 0}
    derived_users = set()
    args.related = 20  # 漫游需要完整相关推荐列表做随机游走
    stop_event = threading.Event()
    install_sigint(stop_event)
    reporter = ProgressReporter(stop_event, lock, stats, total=args.limit, label="roam")
    reporter.start()
    deadline = time.time() + args.max_seconds if args.max_seconds > 0 else None

    def walker(wid):
        q = []
        c = BiliClient(state, interval=args.interval)
        empty_tries = 0
        while True:
            with lock:
                if (stop_event.is_set()
                        or stop_requested(args.stop_file)
                        or (deadline and time.time() >= deadline)
                        or stats["ok"] >= args.limit):
                    if stop_requested(args.stop_file):
                        stop_event.set()
                    return
            item = None
            if q and rng.random() >= args.roam_jump:
                item = q.pop()
            else:
                item = jump.get(c)
                if item:
                    item = ("video", item)
            if item is None and q:
                item = q.pop()
            if not item:
                with lock:
                    if start_pool:
                        item = ("video", start_pool.pop())
            if not item:
                empty_tries += 1
                if empty_tries > 15:
                    return
                continue
            typ, ident = item
            key = f"{typ}:{ident}"
            with lock:
                if key in run_seen or (refresh and key in seen_state
                                       and now - int(seen_state[key]) < refresh):
                    empty_tries += 1
                    if empty_tries > 15:
                        return
                    continue
                run_seen.add(key)
            try:
                if typ == "user":
                    rec, neigh = fetch_author_side(c, ident, args)
                else:
                    rec, neigh = fetch_item(c, typ, ident, args)
            except BiliError as e:
                empty_tries += 1
                with lock:
                    stats["err"] += 1
                if stats["err"] % 25 == 1:
                    print(f"[skip] {key}: {e}")
                continue
            if not rec:
                empty_tries += 1
                if empty_tries > 15:
                    return
                continue
            empty_tries = 0
            with lock:
                if (stop_event.is_set()
                        or stop_requested(args.stop_file)
                        or (deadline and time.time() >= deadline)
                        or stats["ok"] >= args.limit):
                    if stop_requested(args.stop_file):
                        stop_event.set()
                    return
                out_files[rec["type"]].write(json.dumps(rec, ensure_ascii=False) + "\n")
                out_files[rec["type"]].flush()
                seen_state[key] = now
                stats["ok"] += 1
            print(f"[ok] {key} | {rec['title'][:44]}")
            with lock:
                if typ == "video":
                    # 派生 UP 主记录（零额外请求）
                    urec = derive_user_rec(rec)
                    if urec and urec["id"] not in derived_users and urec["id"] not in seen_state:
                        derived_users.add(urec["id"])
                        out_files["user"].write(json.dumps(urec, ensure_ascii=False) + "\n")
                        out_files["user"].flush()
                        # 小概率扩展该作者：收集其专栏/动态
                        if rng.random() < args.author_expand and rec.get("author_id"):
                            q.append(("user", str(rec["author_id"])))
                    vids = [nid for t, nid in neigh if t == "video"]
                    rng.shuffle(vids)
                    for nid in vids[:args.fanout]:
                        k2 = f"video:{nid}"
                        if k2 not in run_seen and k2 not in seen_state and len(q) < args.fanout * 16:
                            q.append(("video", nid))
                else:
                    for nt, nid in neigh:
                        k2 = f"{nt}:{nid}"
                        if k2 not in run_seen and k2 not in seen_state and len(q) < args.fanout * 16:
                            q.append((nt, nid))

    threads = []
    for wid in range(max(1, args.workers)):
        t = threading.Thread(target=walker, args=(wid,), daemon=True)
        t.start()
        threads.append(t)
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        print("\n[已停止] 正在保存状态…", file=sys.stderr)
    finally:
        stop_event.set()
        reporter.join(timeout=1)
        for f in out_files.values():
            f.close()
        state["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _save_state(state, state_path)
    print(f"roam 完成：成功 {stats['ok']}，失败 {stats['err']}；"
          f"跳转源: {','.join(jump.sources)}")
    return 0


def run_continuous(args):
    """24 小时不间断漫游：每 sync-minutes 分钟构建索引并推送到 git。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    stop = threading.Event()
    install_sigint(stop)
    args.limit = 10 ** 9  # 不限条数，由时间片控制
    print(f"[continuous] 24h 不间断漫游开始；每 {args.sync_minutes} 分钟同步一次 git"
          f"（Ctrl+C 优雅停止，再按一次强制退出）")
    round_no = 0
    while not stop.is_set():
        if stop_requested(args.stop_file):
            stop.set()
            break
        round_no += 1
        args.max_seconds = args.sync_minutes * 60
        print(f"[continuous] ── 第 {round_no} 轮漫游（{args.sync_minutes} 分钟）──")
        try:
            run_roam(args)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"[continuous] 本轮异常（继续运行，下轮重试）: {e}")
            continue
        if stop.is_set():
            break
        if stop_requested(args.stop_file):
            stop.set()
            break
        print(f"[continuous] 本轮结束，同步到 git…")
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", "scripts/deploy.ps1", "-Push"],
                check=False,
            )
        except KeyboardInterrupt:
            print("\n[已停止] 同步中断，爬虫退出", file=sys.stderr)
            break
        except Exception as e:
            print(f"[continuous] 同步失败（下一轮会重试）: {e}")
    print("[continuous] 已退出")
    return 0


def run_crawl(args):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = data_dir / "raw"
    raw_dir.mkdir(exist_ok=True)
    state_path = data_dir / "state.json"
    state = {}
    if state_path.exists():
        state = json.loads(state_path.read_text("utf-8"))
    if args.cookie and not state.get("cookie_extra"):
        state["cookie_extra"] = args.cookie
    if args.cookies_file and Path(args.cookies_file).exists() and not state.get("cookie_extra"):
        state["cookie_extra"] = Path(args.cookies_file).read_text("utf-8").strip()
    client = BiliClient(state, interval=args.interval)
    seen_state = state.setdefault("seen", {})
    now = int(time.time())
    refresh = args.refresh_days * 86400

    seeds = load_seeds(args)
    if args.add_popular > 0:
        try:
            for v in client.popular(1, args.add_popular):
                if v.get("bvid"):
                    seeds.append(("video", v["bvid"]))
            print(f"已从热门视频添加 {len(seeds)} 个种子")
        except BiliError as e:
            print(f"[warn] 热门视频获取失败: {e}")
    if not seeds:
        print("没有可用种子：请编辑 seeds.txt 或传 --seed/--add-popular")
        return 1

    out = {}
    stats = {"video": 0, "user": 0, "dynamic": 0, "article": 0}
    errors = 0
    stop_event = threading.Event()
    install_sigint(stop_event)
    lock = threading.Lock()
    reporter = None
    interrupted = False
    try:
        for typ, fname in RAW_FILES.items():
            out[typ] = (raw_dir / fname).open("a", encoding="utf-8")
        queue = deque((t, i, args.depth) for t, i in seeds)
        processed = set()
        reporter = ProgressReporter(stop_event, lock, stats, total=args.limit, label="crawl")
        reporter.start()
        while queue and sum(stats.values()) < args.limit:
            if stop_event.is_set():
                print("[已停止] 提前结束本轮", file=sys.stderr)
                break
            if stop_requested(args.stop_file):
                stop_event.set()
                print("[已停止] 检测到停止标记 data/stop", file=sys.stderr)
                break
            typ, ident, depth = queue.popleft()
            key = f"{typ}:{ident}"
            if key in processed:
                continue
            processed.add(key)
            if refresh and key in seen_state and now - int(seen_state[key]) < refresh:
                continue
            try:
                rec, neigh = fetch_item(client, typ, ident, args)
            except BiliError as e:
                errors += 1
                print(f"[skip] {key}: {e}")
                continue
            if not rec:
                continue
            out[rec["type"]].write(json.dumps(rec, ensure_ascii=False) + "\n")
            out[rec["type"]].flush()
            with lock:
                stats[rec["type"]] += 1
            seen_state[key] = now
            print(f"[ok] {key} | {rec['title'][:48]} | {rec['url']}")
            if depth > 0:
                for nt, nid in neigh:
                    queue.append((nt, nid, depth - 1))
    except KeyboardInterrupt:
        interrupted = True
        print("\n[已停止] 正在保存状态…", file=sys.stderr)
    finally:
        stop_event.set()
        if reporter:
            reporter.join(timeout=1)
        state["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _save_state(state, state_path)
        for f in out.values():
            f.close()
    print(f"{'[已停止] ' if interrupted else ''}完成：本次新增 {sum(stats.values())} 条 {stats}，错误 {errors}")
    return 0


def add_args(parser):
    parser.add_argument("--mode", choices=["crawl", "scheduler", "burst", "roam", "continuous"],
                        default="crawl",
                        help="crawl=图式爬取；burst=批量快抓；roam=全站漫游；continuous=24h 不间断；scheduler=常驻")
    parser.add_argument("--data-dir", default="data", help="数据目录（默认 data）")
    parser.add_argument("--seeds", default="seeds.txt", help="种子文件路径")
    parser.add_argument("--seed", action="append", default=[], help="追加单条种子，可多次")
    parser.add_argument("--add-popular", type=int, default=0,
                        help="先用 N 条热门视频做种子（无种子时引导抓取）")
    parser.add_argument("--depth", type=int, default=1, help="扩展深度（默认 1 跳）")
    parser.add_argument("--limit", type=int, default=300, help="单次最多新增条数")
    parser.add_argument("--interval", type=float, default=1.2, help="请求间隔秒数")
    parser.add_argument("--refresh-days", type=int, default=7,
                        help="多少天内不重复抓取同一实体（默认 7 天）")
    parser.add_argument("--ps", type=int, default=30, help="用户投稿每页条数")
    parser.add_argument("--user-pages", type=int, default=3, help="每个用户最多翻几页")
    parser.add_argument("--neighbor-videos", type=int, default=10)
    parser.add_argument("--neighbor-dynamics", type=int, default=10)
    parser.add_argument("--neighbor-articles", type=int, default=5)
    parser.add_argument("--related", type=int, default=5, help="每个视频最多取几条相关推荐")
    parser.add_argument("--cookie", default="", help="追加 cookie（如 SESSDATA=...）")
    parser.add_argument("--cookies-file", default="data/cookies.txt",
                        help="完整 cookie 文件（gitignore，自动加载；含 SESSDATA 时整体使用）")
    parser.add_argument("--workers", type=int, default=4,
                        help="burst 模式并发 worker 数（默认 4；有 SESSDATA 可到 8-12）")
    parser.add_argument("--bv-file", default=None,
                        help="burst 模式：每行一个 BV/av 的视频列表文件")
    parser.add_argument("--popular", type=int, default=0,
                        help="burst 模式：从热门榜收集 N 个视频")
    parser.add_argument("--ranking", default="",
                        help="burst 模式：按 rid 收集排行榜（逗号分隔，0=全站）")
    parser.add_argument("--roam-jump", type=float, default=0.2,
                        help="roam 模式：每步随机跳转概率（默认 0.2）")
    parser.add_argument("--jump-sources", default="aid,precious,series,popular",
                        help="roam 模式：跳转源（aid/precious/series/popular，逗号分隔）")
    parser.add_argument("--aid-min", type=int, default=1)
    parser.add_argument("--aid-max", type=int, default=150000000,
                        help="roam 模式：随机 av 探测区间上界")
    parser.add_argument("--series-max", type=int, default=200,
                        help="roam 模式：每周必看最大期数")
    parser.add_argument("--fanout", type=int, default=3,
                        help="roam 模式：每步随机选几条相关视频继续游走")
    parser.add_argument("--author-expand", type=float, default=0.05,
                        help="roam 模式：抓到视频后扩展其作者的概率（收集专栏/动态）")
    parser.add_argument("--max-seconds", type=float, default=0,
                        help="roam 模式：单次最多跑多少秒（0=不限）")
    parser.add_argument("--sync-minutes", type=int, default=30,
                        help="continuous 模式：每隔多少分钟构建索引并推送 git")
    parser.add_argument("--stop-file", default="data/stop",
                        help="停止标记文件：存在即安全退出（默认 data/stop）")
    parser.add_argument("--no-lock", action="store_true",
                        help="跳过单实例互斥锁（默认同目录只允许一个爬取进程）")


def main():
    parser = argparse.ArgumentParser(description="BiliSearch 图式爬虫")
    add_args(parser)
    args = parser.parse_args()
    lock = None
    if not args.no_lock:
        lock = acquire_lock(Path(args.data_dir) / "crawler.lock")
        if lock is None:
            print(f"[lock] 已有爬取进程在运行（{args.data_dir}/crawler.lock 被占用），"
                  f"为避免重复爬取已退出；确认没有其他爬虫后删除该文件即可。", file=sys.stderr)
            return 1
    try:
        stop_file = Path(args.stop_file)
        if stop_file.exists():
            stop_file.unlink()  # 清除上次遗留的停止标记
        if args.mode == "burst":
            return run_burst(args)
        if args.mode == "roam":
            return run_roam(args)
        if args.mode == "continuous":
            return run_continuous(args)
        return run_crawl(args)
    finally:
        if lock:
            try:
                lock.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
