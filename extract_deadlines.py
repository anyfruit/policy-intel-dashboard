#!/usr/bin/env python3
"""
extract_deadlines.py
从政策标题/summary/content 中用正则提取"征求意见截止日期"，
写入 items.deadline_date（YYYY-MM-DD）。

用法:
    python extract_deadlines.py            # 处理所有"征求意见"状态条目
    python extract_deadlines.py --all      # 处理全部条目
    python extract_deadlines.py --dry-run  # 只打印，不写库
"""

import argparse
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from db import DB_PATH, init_db

# ── 正则模式 ──────────────────────────────────────────────
# 覆盖常见写法:
#   截止时间/日期/公示 YYYY年MM月DD日
#   至 YYYY年MM月DD日前
#   YYYY-MM-DD
#   YYYY.MM.DD
#   20YYMMDD（8位纯数字）

_PATTERNS = [
    # "截止时间/日期 YYYY年MM月DD日" - 最严格，上下文必须有"截止"
    r"截止(?:时间|日期|反馈)?[：:为至\s]*(\d{4})[年\-\./](\d{1,2})[月\-\./](\d{1,2})日?",
    # "截至 YYYY年MM月DD日"
    r"截至(\d{4})[年\-\./](\d{1,2})[月\-\./](\d{1,2})日?",
    # "意见反馈截止日 YYYY年MM月DD日"
    r"(?:征求意见|公示|意见反馈|反馈意见)[^。\n]{0,30}截(?:止|至)[^\d]*(\d{4})[年\-\./](\d{1,2})[月\-\./](\d{1,2})日?",
    # "至YYYY年MM月DD日前" (意见截止)
    r"至(\d{4})年(\d{1,2})月(\d{1,2})日前(?:提交|反馈|报送|征求)",
    # "deadline: YYYY-MM-DD"
    r"(?i)deadline[^\d]*(\d{4})[-\./](\d{2})[-\./](\d{2})",
]

_COMPILED = [re.compile(p, re.DOTALL | re.IGNORECASE) for p in _PATTERNS]


def extract_deadline(text: str) -> str | None:
    """从文本中提取第一个有效截止日期，返回 'YYYY-MM-DD' 或 None"""
    if not text:
        return None
    for pat in _COMPILED:
        m = pat.search(text)
        if m:
            try:
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if 2020 <= y <= 2035 and 1 <= mo <= 12 and 1 <= d <= 31:
                    return f"{y:04d}-{mo:02d}-{d:02d}"
            except (IndexError, ValueError):
                continue
    return None


def run(all_items: bool = False, dry_run: bool = False):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    where = "" if all_items else "WHERE status = '征求意见'"
    rows = conn.execute(
        f"SELECT id, title, summary, content, deadline_date FROM items {where}"
    ).fetchall()

    print(f"📋 扫描 {len(rows)} 条记录…")
    updated = 0
    for row in rows:
        item_id = row["id"]
        combined = " ".join(filter(None, [
            row["title"] or "",
            row["summary"] or "",
            (row["content"] or "")[:2000],  # 只看前2000字，加速
        ]))
        deadline = extract_deadline(combined)
        if deadline and deadline != row["deadline_date"]:
            print(f"  [{item_id}] {row['title'][:40]!r:42}  → {deadline}")
            if not dry_run:
                conn.execute(
                    "UPDATE items SET deadline_date=? WHERE id=?",
                    (deadline, item_id)
                )
            updated += 1

    if not dry_run:
        conn.commit()
    conn.close()
    print(f"\n✅ {'(dry-run) ' if dry_run else ''}更新 {updated} 条截止日期")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--all", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    run(all_items=args.all, dry_run=args.dry_run)
