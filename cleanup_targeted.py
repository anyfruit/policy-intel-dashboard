#!/usr/bin/env python3
"""cleanup_targeted.py — 精准清理：基于来源 + 相关性

策略（比 aggressive 精准得多）：
1. 应急管理部 mem_gk → 全删（样本0%储能相关）
2. 发改委/上海等泛政务来源 → 保留储能相关，删无关的
3. 搜索来源 / 专业媒体来源 → 全保留
4. 黑名单词 → 全删（同 cleanup_noise.py）

数据：
  - mem_gk: 749条, 0%相关 → 全删
  - ndrc_xxgk_zcfb: 1378条, 14%相关 → 删~1185条
  - shanghai_fgw_policy: 1846条, 12%相关 → 删~1624条
  预计净减少 ~3558 条，保留高质量核心
"""
from __future__ import annotations
import argparse, json, re
from datetime import datetime
from pathlib import Path
from db import get_conn, DB_PATH
from tags import is_storage_relevant

# 完全不相关来源 → 全删
FULL_DELETE_SOURCES = [
    "mem_gk",           # 应急管理部 - 与储能无关
]

# 泛政务来源 → 只保留储能相关
FILTER_SOURCES = [
    "ndrc_xxgk_zcfb",      # 国家发改委
    "shanghai_fgw_policy", # 上海市发改委
    "nea_zwgk_policy",     # 国家能源局（47%相关，只保留相关的）
]

# 黑名单词（标题命中即删）
BLACKLIST = [
    r"成品油", r"汽油", r"柴油", r"油价",
    r"液化石油气", r"燃气(?!储能)", r"天然气",
    r"社会信用", r"信用体系", r"收费目录清单",
    r"生活垃圾", r"垃圾焚烧", r"养老机构", r"民政局",
    r"科研课题", r"合作单位征集", r"殡葬",
    r"化肥", r"春耕", r"农业", r"种子", r"粮食",
    r"城际铁路", r"低空经济", r"生物柴油",
]

def is_blacklist(title, summary):
    t = (title or "") + " " + (summary or "")
    return any(re.search(p, t, re.IGNORECASE) for p in BLACKLIST)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=30)
    args = ap.parse_args()
    if args.apply and args.dry_run:
        raise SystemExit("不能同时 --apply 和 --dry-run")

    conn = get_conn()
    to_delete = []
    reasons = {}
    try:
        rows = conn.execute(
            "SELECT id, source_id, title, summary FROM items"
        ).fetchall()

        for r in rows:
            rid, src, title, summary = r
            text = (title or "") + " " + (summary or "")

            # 1) 黑名单词
            if is_blacklist(title, summary):
                to_delete.append(rid)
                reasons[rid] = "blacklist"
                continue

            # 2) 完全删除的来源
            if src in FULL_DELETE_SOURCES:
                to_delete.append(rid)
                reasons[rid] = f"full_delete_src:{src}"
                continue

            # 3) 泛政务来源 - 过相关性
            if src in FILTER_SOURCES:
                if not is_storage_relevant(text):
                    to_delete.append(rid)
                    reasons[rid] = f"irrelevant:{src}"
                    continue

        print(f"DB: {DB_PATH}")
        print(f"总量: {len(rows)}, 待删: {len(to_delete)}")
        by_reason = {}
        for v in reasons.values():
            k = v.split(":")[0]
            by_reason[k] = by_reason.get(k, 0) + 1
        for k, n in sorted(by_reason.items(), key=lambda x: -x[1]):
            print(f"  {k}: {n}")

        if not to_delete:
            print("无需清理")
            return

        print(f"\n预览（前{args.limit}条）:")
        sample = [r for r in rows if r[0] in set(to_delete[:args.limit*3])][:args.limit]
        for r in sample:
            print(f"  [{reasons.get(r[0],'')}] {(r[2] or '')[:55]}")

        if not args.apply:
            print(f"\n实际删除请加 --apply")
            return

        # 备份
        bk_dir = Path(__file__).parent / "data" / "cleanup_backups"
        bk_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        bk = bk_dir / f"targeted_backup_{ts}.jsonl"
        del_ids = set(to_delete)
        bk_rows = [r for r in conn.execute("SELECT id,source_id,title,summary,region,date FROM items").fetchall() if r[0] in del_ids]
        with bk.open("w") as f:
            for r in bk_rows:
                f.write(json.dumps({"id":r[0],"source_id":r[1],"title":r[2],"summary":r[3],"region":r[4],"date":r[5],"reason":reasons.get(r[0],"")}, ensure_ascii=False) + "\n")

        qm = ",".join(["?"] * len(to_delete))
        for tbl in ("item_versions","item_analyses","bookmarks","notifications"):
            try:
                conn.execute(f"DELETE FROM {tbl} WHERE item_id IN ({qm})", to_delete)
            except Exception:
                pass
        conn.execute(f"DELETE FROM items WHERE id IN ({qm})", to_delete)
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        print(f"\n✅ 删除 {len(to_delete)} 条，备份→ {bk}\n剩余: {after} 条")
    finally:
        conn.close()

if __name__ == "__main__":
    main()
