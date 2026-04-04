#!/usr/bin/env python3
"""auto_scrape.py — 全自动每日数据更新脚本

用法：
  python3 auto_scrape.py           # 全量（月报 + 省份搜索 + 清理）
  python3 auto_scrape.py --monthly # 只跑月报
  python3 auto_scrape.py --search  # 只跑省份搜索
  python3 auto_scrape.py --clean   # 只跑噪音清理

fly.io 用 --scheduled 每天执行，部署为单独进程组（cron = true）。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from db import init_db, get_conn
from scrape_in_en import MONTHLY_URLS, scrape_url
from scrape_provinces_via_search import PROVINCE_QUERIES, process_province, BRAVE_API_KEY
from cleanup_noise import is_noise
from extract_deadlines import run as _run_deadline_extraction


# ── 额外省份：目前搜索脚本里已有，但可补充 ────────────────────────────
EXTRA_PROVINCE_QUERIES = [
    ("湖北", [
        "湖北省 储能政策 发改委 2024 OR 2025 OR 2026",
        "湖北 独立储能 site:chuneng.bjx.com.cn",
    ], "hubei_search"),
    ("河南", [
        "河南省 储能政策 发改委 2024 OR 2025 OR 2026",
        "河南 独立储能 site:chuneng.bjx.com.cn",
    ], "henan_search"),
    ("山东", [
        "山东省 储能政策 电网 2024 OR 2025 OR 2026",
        "山东 新型储能 site:chuneng.bjx.com.cn OR site:in-en.com",
    ], "shandong_search"),
    ("浙江", [
        "浙江省 储能政策 发改委 2024 OR 2025 OR 2026",
        "浙江 新型储能 site:chuneng.bjx.com.cn",
    ], "zhejiang_search"),
    ("广西", [
        "广西壮族自治区 储能政策 2024 OR 2025 OR 2026",
        "广西 新型储能 site:chuneng.bjx.com.cn",
    ], "guangxi_search"),
    ("云南", [
        "云南省 储能政策 发改委 2024 OR 2025 OR 2026",
        "云南 储能电站 site:chuneng.bjx.com.cn",
    ], "yunnan_search"),
    ("贵州", [
        "贵州省 储能政策 2024 OR 2025 OR 2026",
        "贵州 新型储能 site:chuneng.bjx.com.cn",
    ], "guizhou_search"),
]


def run_monthly(dry_run: bool = False) -> int:
    print(f"\n{'='*50}\n📰 [月报] 国际储能网月报抓取\n{'='*50}")
    total = 0
    for ym, url in MONTHLY_URLS[:3]:  # 只抓最近3期，避免重复浪费
        n = scrape_url(url, ym, dry_run=dry_run)
        total += n
        time.sleep(1)
    print(f"月报完成，处理 {total} 条")
    return total


def run_province_search(dry_run: bool = False) -> int:
    if not BRAVE_API_KEY:
        print("⚠️  BRAVE_API_KEY 未设置，跳过省份搜索")
        return 0

    print(f"\n{'='*50}\n🗺️  [省份搜索] Brave Search 省级政策\n{'='*50}")
    all_queries = list(PROVINCE_QUERIES) + EXTRA_PROVINCE_QUERIES
    total = 0
    for province, queries, source_id in all_queries:
        n = process_province(province, queries, source_id, dry_run=dry_run)
        total += n
        time.sleep(1.5)
    print(f"省份搜索完成，处理 {total} 条")
    return total


def run_deadline_extraction(dry_run: bool = False) -> None:
    print(f"\n{'='*50}\n📅 [截止日期] 自动提取征求意见截止日期\n{'='*50}")
    _run_deadline_extraction(all_items=False, dry_run=dry_run)


def run_cleanup() -> int:
    print(f"\n{'='*50}\n🧹 [清理] 噪音过滤\n{'='*50}")
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id,title,summary,source_name FROM items"
        ).fetchall()

        noise_ids = []
        for r in rows:
            ok, _ = is_noise(r[1], r[2], r[3], aggressive=False)
            if ok:
                noise_ids.append(r[0])

        if not noise_ids:
            print("✅ 无噪音")
            return 0

        qm = ",".join(["?"] * len(noise_ids))
        for tbl in ("item_versions", "item_analyses", "bookmarks", "notifications"):
            try:
                conn.execute(f"DELETE FROM {tbl} WHERE item_id IN ({qm})", noise_ids)
            except Exception:
                pass
        conn.execute(f"DELETE FROM items WHERE id IN ({qm})", noise_ids)
        conn.commit()

        after = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        print(f"清理 {len(noise_ids)} 条噪音，剩余 {after} 条")
        return len(noise_ids)
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--monthly", action="store_true")
    ap.add_argument("--search", action="store_true")
    ap.add_argument("--clean", action="store_true")
    ap.add_argument("--deadlines", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    init_db()
    start = datetime.now()
    print(f"\n🚀 auto_scrape 开始 {start.strftime('%Y-%m-%d %H:%M:%S')}")

    run_all = not (args.monthly or args.search or args.clean or args.deadlines)

    total_new = 0
    if run_all or args.monthly:
        total_new += run_monthly(dry_run=args.dry_run)

    if run_all or args.search:
        total_new += run_province_search(dry_run=args.dry_run)

    if run_all or args.deadlines:
        run_deadline_extraction(dry_run=args.dry_run)

    if (run_all or args.clean) and not args.dry_run:
        run_cleanup()

    conn = get_conn()
    try:
        final_count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    finally:
        conn.close()

    elapsed = (datetime.now() - start).total_seconds()
    print(f"\n✅ 完成！新增/处理 {total_new} 条，DB 总量 {final_count} 条，耗时 {elapsed:.0f}s")


if __name__ == "__main__":
    main()
