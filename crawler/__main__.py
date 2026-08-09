"""命令行入口：单次爬取或常驻调度器。"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

from .crawl import acquire_lock, add_args, run_burst, run_continuous, run_crawl, run_roam


def build(args):
    subprocess.run(
        [sys.executable, "build_index.py", "--raw", f"{args.data_dir}/raw",
         "--out", args.site_data],
        check=False,
    )


def scheduler_loop(args):
    print(f"[scheduler] 常驻模式：每 {args.interval_hours} 小时执行一次")
    while True:
        started = time.time()
        try:
            rc = run_crawl(args)
            if args.build and rc == 0:
                build(args)
        except Exception as e:  # noqa: BLE001 - 常驻进程不允许中断
            print(f"[scheduler] 本轮错误: {e}")
        elapsed = time.time() - started
        sleep = max(60, args.interval_hours * 3600 - elapsed)
        print(f"[scheduler] 本轮耗时 {elapsed:.0f}s，{sleep / 3600:.1f} 小时后再次执行")
        time.sleep(sleep)


def main():
    parser = argparse.ArgumentParser(
        description="BiliSearch：本地定时爬取 B 站元数据并构建离线索引")
    add_args(parser)
    parser.add_argument("--build", action="store_true",
                        help="爬取完成后运行 build_index.py 构建站点索引")
    parser.add_argument("--interval-hours", type=float, default=6.0,
                        help="scheduler 模式下两次爬取间隔（小时）")
    parser.add_argument("--site-data", default="site/data",
                        help="索引输出目录（默认 site/data）")
    args = parser.parse_args()
    lock = None
    if not args.no_lock:
        lock = acquire_lock(Path(args.data_dir) / "crawler.lock")
        if lock is None:
            print(f"[lock] 已有爬取进程在运行（{args.data_dir}/crawler.lock 被占用），"
                  f"为避免重复爬取已退出；确认没有其他爬虫后删除该文件即可。", file=sys.stderr)
            return 1
    try:
        if args.mode == "burst":
            rc = run_burst(args)
            if args.build and rc == 0:
                build(args)
            return rc
        if args.mode == "roam":
            rc = run_roam(args)
            if args.build and rc == 0:
                build(args)
            return rc
        if args.mode == "continuous":
            return run_continuous(args)
        if args.mode == "scheduler":
            scheduler_loop(args)
            return 0
        rc = run_crawl(args)
        if args.build and rc == 0:
            build(args)
        return rc
    finally:
        if lock:
            try:
                lock.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
