#!/usr/bin/env python3
"""cleanup_noise.py — 删除“噪音政策”条目（可回滚）

点点的核心诉求：
- DB 里不要混入明显不相关的政策（油价/社会信用/垃圾/养老等）
- 删除前必须备份（jsonl），可按 id 恢复

用法：
  python3 cleanup_noise.py --dry-run
  python3 cleanup_noise.py --apply

策略：
1) 黑名单：命中即判噪音
2) 相关性：若 title+summary+source_name 都不包含储能/电力市场关键词，且来源属于“泛发改委/综合政务”，判噪音

"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path

from db import get_conn, DB_PATH
from tags import is_storage_relevant


BLACKLIST_PATTERNS = [
    r"成品油", r"汽油", r"柴油", r"油价", r"天然气", r"液化石油气", r"燃气",
    r"社会信用", r"信用体系", r"收费目录清单", r"价格调整",
    r"生活垃圾", r"垃圾焚烧", r"养老机构", r"民政局",
    r"科研课题", r"合作单位",
]

# “泛政策站”来源：更容易混入噪音（命中相关性弱时直接删）
GENERIC_SOURCE_HINTS = [
    "发改委", "发展和改革委员会", "政务公开", "通知公告", "价格", "信用", "生态环境",
]


def is_noise(title: str, summary: str, source_name: str, aggressive: bool = False) -> tuple[bool, str]:
    text = " ".join([(title or ""), (summary or ""), (source_name or "")]).strip()
    if not text:
        return False, "empty"

    for p in BLACKLIST_PATTERNS:
        if re.search(p, text, re.IGNORECASE):
            return True, f"blacklist:{p}"

    # 可选：激进模式（会删掉“泛发改委/综合政务”里与储能无关的内容，风险更高）
    if aggressive:
        rel = is_storage_relevant(text)
        if not rel:
            if any(h in (source_name or "") for h in GENERIC_SOURCE_HINTS):
                return True, "irrelevant+generic_source"

    return False, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只统计/预览，不删除")
    ap.add_argument("--apply", action="store_true", help="执行删除")
    ap.add_argument("--limit", type=int, default=50, help="预览显示条数")
    ap.add_argument("--aggressive", action="store_true", help="激进清理：会删除泛来源中与储能无关的内容（有误删风险）")
    args = ap.parse_args()

    if args.apply and args.dry_run:
        raise SystemExit("--apply 和 --dry-run 只能选一个")

    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id,url,title,summary,region,source_name,date,created_at FROM items"
        ).fetchall()

        noise = []
        for r in rows:
            ok, reason = is_noise(r[2], r[3], r[5], aggressive=args.aggressive)
            if ok:
                noise.append({
                    "id": r[0],
                    "url": r[1],
                    "title": r[2],
                    "summary": (r[3] or "")[:240],
                    "region": r[4],
                    "source_name": r[5],
                    "date": r[6],
                    "created_at": r[7],
                    "reason": reason,
                })

        total = len(rows)
        n_noise = len(noise)
        print(f"DB: {DB_PATH}\nitems total={total} | noise candidates={n_noise}")

        if n_noise == 0:
            return

        print("\n预览(前 {} 条):".format(min(args.limit, n_noise)))
        for x in noise[: args.limit]:
            print(f"- #{x['id']} [{x['region']}] {x['date'] or '—'} {x['title'][:60]} ({x['reason']})")

        if args.dry_run or (not args.apply):
            if not args.apply:
                print("\n未执行删除（如需删除：加 --apply）")
            return

        # 备份
        bk_dir = Path(__file__).parent / "data" / "cleanup_backups"
        bk_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        bk_path = bk_dir / f"noise_backup_{ts}.jsonl"
        with bk_path.open("w", encoding="utf-8") as f:
            for x in noise:
                f.write(json.dumps(x, ensure_ascii=False) + "\n")

        # 删除（先删子表，避免外键约束失败）
        ids = [x["id"] for x in noise]
        qmarks = ",".join(["?"] * len(ids))
        if ids:
            # item_versions
            conn.execute(f"DELETE FROM item_versions WHERE item_id IN ({qmarks})", ids)
            # analyses / bookmarks / notifications（这些表可能在旧库里不存在，所以 try）
            for tbl in ("item_analyses", "bookmarks", "notifications"):
                try:
                    conn.execute(f"DELETE FROM {tbl} WHERE item_id IN ({qmarks})", ids)
                except Exception:
                    pass
            # 最后删 items
            conn.execute(f"DELETE FROM items WHERE id IN ({qmarks})", ids)
        conn.commit()

        after = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        print(f"\n已删除 {n_noise} 条，备份：{bk_path}\n删除后 items={after}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
