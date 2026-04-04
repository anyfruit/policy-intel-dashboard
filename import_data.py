"""import_data.py — 从 Fly.io 备份数据库导入到 Railway 版本数据库

用法:
  BACKUP_DB=./policy_flyio_backup.db DB_PATH=./policy_local.db python import_data.py

默认:
  BACKUP_DB  = ../policy_flyio_backup.db (相对脚本目录)
  DB_PATH    = 环境变量 DB_PATH 或 /data/policy.db
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

# ── 路径配置 ──────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent

# 备份库路径：优先用环境变量，否则找项目根目录
_default_backup = SCRIPT_DIR / "policy_flyio_backup.db"
if not _default_backup.exists():
    _default_backup = SCRIPT_DIR.parent / "policy_flyio_backup.db"

BACKUP_DB = Path(os.getenv("BACKUP_DB", str(_default_backup)))
DB_PATH   = Path(os.getenv("DB_PATH", "/data/policy.db"))

# ── 初始化目标库 ──────────────────────────────────────────────────────────────

def init_target(conn: sqlite3.Connection) -> None:
    conn.executescript("""
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS items (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  url           TEXT    UNIQUE NOT NULL,
  title         TEXT    NOT NULL,
  summary       TEXT,
  content       TEXT,
  date          TEXT,
  region        TEXT,
  source_id     TEXT,
  source_name   TEXT,
  categories    TEXT    DEFAULT '[]',
  tags          TEXT    DEFAULT '[]',
  impact_on     TEXT    DEFAULT '[]',
  level         TEXT    DEFAULT '国家',
  status        TEXT    DEFAULT '现行',
  deadline_date TEXT,
  created_at    TEXT    DEFAULT (datetime('now','localtime')),
  updated_at    TEXT    DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_items_date     ON items(date);
CREATE INDEX IF NOT EXISTS idx_items_region   ON items(region);
CREATE INDEX IF NOT EXISTS idx_items_level    ON items(level);
CREATE INDEX IF NOT EXISTS idx_items_status   ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_source   ON items(source_id);
CREATE INDEX IF NOT EXISTS idx_items_updated  ON items(updated_at);
""")


# ── JSON 字段归一化 ───────────────────────────────────────────────────────────

def _norm_json(v) -> str:
    """统一转成 JSON 字符串列表。"""
    if v is None:
        return "[]"
    if isinstance(v, list):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return "[]"
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return json.dumps(parsed, ensure_ascii=False)
            return json.dumps([str(parsed)], ensure_ascii=False)
        except Exception:
            # 不是合法 JSON，当作纯字符串
            return json.dumps([s], ensure_ascii=False)
    return json.dumps([str(v)], ensure_ascii=False)


# ── 主导入逻辑 ────────────────────────────────────────────────────────────────

def import_data() -> None:
    if not BACKUP_DB.exists():
        print(f"[错误] 备份数据库不存在: {BACKUP_DB}", file=sys.stderr)
        sys.exit(1)

    print(f"[info] 备份库: {BACKUP_DB}")
    print(f"[info] 目标库: {DB_PATH}")

    # 打开备份库
    src = sqlite3.connect(str(BACKUP_DB))
    src.row_factory = sqlite3.Row

    # 打开/创建目标库
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    dst = sqlite3.connect(str(DB_PATH))
    dst.row_factory = sqlite3.Row
    init_target(dst)

    # 查询备份库中所有条目
    rows = src.execute("SELECT * FROM items").fetchall()
    print(f"[info] 备份库共 {len(rows)} 条记录，开始导入...")

    inserted = 0
    skipped  = 0
    errors   = 0

    for row in rows:
        r = dict(row)
        url = (r.get("url") or "").strip()
        if not url:
            errors += 1
            continue

        title = (r.get("title") or "").strip()
        if not title:
            # 从 raw_json 尝试取 title
            try:
                rj = json.loads(r.get("raw_json") or "{}")
                title = (rj.get("title") or "").strip()
            except Exception:
                pass
        if not title:
            errors += 1
            continue

        # 字段映射
        data = {
            "url":          url,
            "title":        title[:500],
            "summary":      (r.get("summary") or r.get("summary_1line") or "")[:2000],
            "content":      (r.get("content") or "")[:10000],
            "date":         r.get("date"),
            "region":       r.get("region"),
            "source_id":    r.get("source_id"),
            "source_name":  r.get("source_name"),
            "categories":   _norm_json(r.get("categories")),
            "tags":         _norm_json(r.get("tags")),
            "impact_on":    _norm_json(r.get("impact_on")),
            "level":        r.get("level") or "国家",
            "status":       r.get("status") or "现行",
            "deadline_date": r.get("deadline_date"),
            "created_at":   r.get("created_at"),
        }

        try:
            # INSERT OR IGNORE：按 URL 去重，已存在则跳过
            cur = dst.execute(
                """INSERT OR IGNORE INTO items
                   (url, title, summary, content, date, region, source_id, source_name,
                    categories, tags, impact_on, level, status, deadline_date, created_at)
                   VALUES (:url, :title, :summary, :content, :date, :region, :source_id,
                           :source_name, :categories, :tags, :impact_on, :level, :status,
                           :deadline_date, :created_at)""",
                data,
            )
            if cur.rowcount > 0:
                inserted += 1
            else:
                skipped += 1
        except Exception as e:
            print(f"[warn] 插入失败 ({url[:60]}): {e}")
            errors += 1

    dst.commit()
    src.close()
    dst.close()

    print(f"\n[完成] 导入结果：")
    print(f"  ✓ 新增：{inserted} 条")
    print(f"  - 跳过（已存在）：{skipped} 条")
    print(f"  ✗ 错误：{errors} 条")
    print(f"  共处理：{len(rows)} 条")


if __name__ == "__main__":
    import_data()
