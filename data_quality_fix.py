#!/usr/bin/env python3
"""data_quality_fix.py — 一次性数据质量修复脚本

功能：
1. 删除广东来源中与储能无关的节能报告审查意见等条目
2. 删除浙江来源中的教育/政务类噪音
3. 清除 title/summary 中的 HTML 标签
4. 更新 source_name，添加来源类型标注（[官方] / [行业媒体] / [搜索]）
5. 删除重复标题条目（保留较早的一条）
6. 重新运行 infer_categories/extract_tags/infer_level/infer_status 更新所有条目的分类

用法：
  python3 data_quality_fix.py --dry-run
  python3 data_quality_fix.py --apply
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from tags import infer_categories, extract_tags, infer_level, infer_status, is_storage_relevant

DB = "seed.db"


# ─── HTML 清理 ───────────────────────────────────────────────────────────────
_HTML_TAG = re.compile(r"<[^>]+>")
_HTML_ENTITY = re.compile(r"&(?:amp|lt|gt|nbsp|quot|apos);", re.IGNORECASE)
_ENTITY_MAP = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&nbsp;": " ", "&quot;": '"', "&apos;": "'"}


def strip_html(text: str | None) -> str:
    if not text:
        return text or ""
    cleaned = _HTML_TAG.sub("", text)
    for ent, rep in _ENTITY_MAP.items():
        cleaned = cleaned.replace(ent, rep)
    return cleaned.strip()


# ─── source_name 类型标注 ─────────────────────────────────────────────────────
_SOURCE_TYPE_MAP = {
    "国家能源局-政务公开-政策": "[官方] 国家能源局-政务公开-政策",
    "国家发改委-政务公开-政策发布": "[官方] 国家发改委-政务公开-政策发布",
    "广东省发改委/能源局-通知公告（含储能/新能源/节能）": "[官方] 广东省发改委/能源局-通知公告（含储能/新能源/节能）",
    "浙江省发改委-通知公告（含新能源/储能）": "[官方] 浙江省发改委-通知公告（含新能源/储能）",
    "上海市发改委-政策文件（含补贴/能源/价格等）": "[官方] 上海市发改委-政策文件（含补贴/能源/价格等）",
    "山东省发改委-通知公告": "[官方] 山东省发改委-通知公告",
    "国际储能网-月度政策汇总": "[行业媒体] 国际储能网-月度政策汇总",
}
# 搜索来源：source_name 以"XX省政策（搜索来源）"结尾的统一加前缀


def normalize_source_name(sn: str) -> str:
    if not sn:
        return sn
    # 已经有标注的不重复处理
    if sn.startswith("[官方]") or sn.startswith("[行业媒体]") or sn.startswith("[搜索]"):
        return sn
    if sn in _SOURCE_TYPE_MAP:
        return _SOURCE_TYPE_MAP[sn]
    if "搜索来源" in sn:
        return f"[搜索] {sn}"
    return sn


# ─── 噪音过滤 ─────────────────────────────────────────────────────────────────
def find_noise_ids(conn: sqlite3.Connection) -> list[int]:
    """返回要删除的噪音条目 ID 列表"""
    noise: list[int] = []

    # 广东来源：不含储能/新能源/电力关键词的条目
    gd_rows = conn.execute(
        "SELECT id, title, summary FROM items WHERE source_name LIKE '广东省发改委%'"
    ).fetchall()
    for r in gd_rows:
        text = (r[1] or "") + " " + (r[2] or "")
        if not is_storage_relevant(text):
            noise.append(r[0])

    # 浙江来源：明显非能源类（教育/行政）
    zj_rows = conn.execute(
        "SELECT id, title FROM items WHERE source_name LIKE '浙江省发改委%'"
    ).fetchall()
    for r in zj_rows:
        title = r[1] or ""
        if not is_storage_relevant(title) and re.search(
            r"学费|教材|美术学院|工商大学|联合学院|诺丁汉|宁波大学昂热|艺术学院", title
        ):
            noise.append(r[0])

    return sorted(set(noise))


def find_duplicate_ids(conn: sqlite3.Connection) -> list[int]:
    """返回重复标题中要删除的 ID（保留 min(id) 的一条）"""
    rows = conn.execute("""
        SELECT title, GROUP_CONCAT(id ORDER BY id) as ids
        FROM items GROUP BY title HAVING COUNT(*) > 1
    """).fetchall()
    to_delete = []
    for r in rows:
        ids = [int(x) for x in r[1].split(",")]
        to_delete.extend(ids[1:])  # 保留最小 id
    return to_delete


# ─── 主流程 ────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    if not args.apply and not args.dry_run:
        args.dry_run = True  # 默认 dry-run

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # 1. 找噪音
    noise_ids = find_noise_ids(conn)
    print(f"[1] 待删噪音: {len(noise_ids)} 条")
    for nid in noise_ids[:10]:
        r = conn.execute("SELECT id, title FROM items WHERE id=?", [nid]).fetchone()
        if r:
            print(f"    #{r['id']} {(r['title'] or '')[:80]}")
    if len(noise_ids) > 10:
        print(f"    ... 共 {len(noise_ids)} 条")

    # 2. 找重复
    dup_ids = find_duplicate_ids(conn)
    print(f"[2] 待删重复: {len(dup_ids)} 条")

    # 3. HTML 需清理的条目
    html_tag = re.compile(r"<[a-zA-Z/]")
    all_rows = conn.execute("SELECT id, title, summary FROM items").fetchall()
    html_items = [(r["id"], strip_html(r["title"]), strip_html(r["summary"]))
                  for r in all_rows
                  if (r["title"] and html_tag.search(r["title"]))
                  or (r["summary"] and html_tag.search(r["summary"]))]
    print(f"[3] 含 HTML 标签: {len(html_items)} 条")

    # 4. source_name 更新
    sn_rows = conn.execute("SELECT DISTINCT source_name FROM items").fetchall()
    sn_updates = [(normalize_source_name(r["source_name"]), r["source_name"])
                  for r in sn_rows
                  if normalize_source_name(r["source_name"]) != r["source_name"]]
    print(f"[4] source_name 需更新: {len(sn_updates)} 种")
    for new, old in sn_updates:
        cnt = conn.execute("SELECT COUNT(*) FROM items WHERE source_name=?", [old]).fetchone()[0]
        print(f"    {old!r} → {new!r} ({cnt} 条)")

    # 5. 分类重跑
    print(f"[5] 将对剩余条目重跑分类标签")

    if args.dry_run:
        print("\n[dry-run 模式，未写入数据库]")
        conn.close()
        return

    # ── APPLY ──────────────────────────────────────────────────────────────
    bk_dir = Path("data/cleanup_backups")
    bk_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 备份要删除的条目
    all_del_ids = sorted(set(noise_ids + dup_ids))
    if all_del_ids:
        qm = ",".join(["?"] * len(all_del_ids))
        del_rows = conn.execute(f"SELECT * FROM items WHERE id IN ({qm})", all_del_ids).fetchall()
        bk_path = bk_dir / f"dq_deleted_{ts}.jsonl"
        with bk_path.open("w", encoding="utf-8") as f:
            for r in del_rows:
                f.write(json.dumps(dict(r), ensure_ascii=False, default=str) + "\n")
        conn.execute(f"DELETE FROM items WHERE id IN ({qm})", all_del_ids)
        print(f"已删除 {len(all_del_ids)} 条，备份: {bk_path}")

    # HTML 清理
    for item_id, new_title, new_summary in html_items:
        conn.execute(
            "UPDATE items SET title=?, summary=? WHERE id=?",
            [new_title, new_summary, item_id]
        )
    print(f"已清理 {len(html_items)} 条 HTML 标签")

    # source_name 更新
    for new_sn, old_sn in sn_updates:
        conn.execute("UPDATE items SET source_name=? WHERE source_name=?", [new_sn, old_sn])
    print(f"已更新 {len(sn_updates)} 种 source_name")

    # 重跑分类
    remaining = conn.execute("SELECT id, title, summary, source_id FROM items").fetchall()
    updated = 0
    for r in remaining:
        text = (r["title"] or "") + " " + (r["summary"] or "")
        cats = json.dumps(infer_categories(text), ensure_ascii=False)
        tgs = json.dumps(extract_tags(text), ensure_ascii=False)
        lv = infer_level(r["source_id"] or "", r["title"] or "")
        st = infer_status(text)
        conn.execute(
            "UPDATE items SET categories=?, tags=?, level=?, status=? WHERE id=?",
            [cats, tgs, lv, st, r["id"]]
        )
        updated += 1
    print(f"已重跑分类: {updated} 条")

    conn.commit()
    final = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    print(f"\n完成。最终 items 数: {final}")
    conn.close()


if __name__ == "__main__":
    main()
