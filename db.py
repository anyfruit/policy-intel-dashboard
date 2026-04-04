"""db.py — SQLite 数据库层

所有模块的唯一数据库入口。
- get_conn()               → 返回 sqlite3.Connection（调用方负责 close）
- init_db()                → 建表/建索引
- upsert_item_with_version → 插入或更新条目（含版本历史 + 标题归一化去重）
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from typing import Optional

DB_PATH = os.getenv("DB_PATH", "/data/policy.db")

_CREATE_SCHEMA = """
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
  canonical_id  INTEGER,
  created_at    TEXT    DEFAULT (datetime('now','localtime')),
  updated_at    TEXT    DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS item_versions (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id    INTEGER NOT NULL,
  old_data   TEXT,
  new_data   TEXT,
  changed_at TEXT    DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS item_analyses (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id        INTEGER NOT NULL UNIQUE,
  analysis_type  TEXT    DEFAULT 'ai_interpret',
  analysis_data  TEXT,
  created_at     TEXT    DEFAULT (datetime('now','localtime')),
  updated_at     TEXT    DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  username      TEXT    UNIQUE NOT NULL,
  password_hash TEXT    NOT NULL,
  salt          TEXT    NOT NULL,
  email         TEXT    DEFAULT '',
  is_paid       INTEGER DEFAULT 0,
  plan          TEXT    DEFAULT 'free',
  created_at    TEXT    DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS bookmarks (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id    INTEGER NOT NULL,
  item_id    INTEGER NOT NULL,
  created_at TEXT    DEFAULT (datetime('now','localtime')),
  UNIQUE(user_id, item_id)
);

CREATE TABLE IF NOT EXISTS notifications (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id           INTEGER NOT NULL,
  item_id           INTEGER NOT NULL,
  subscription_name TEXT,
  created_at        TEXT    DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS subscriptions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id         INTEGER NOT NULL UNIQUE,
  frequency       TEXT    DEFAULT 'weekly',
  filter_region   TEXT    DEFAULT '',
  filter_category TEXT    DEFAULT '',
  created_at      TEXT    DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_items_date       ON items(date);
CREATE INDEX IF NOT EXISTS idx_items_region     ON items(region);
CREATE INDEX IF NOT EXISTS idx_items_level      ON items(level);
CREATE INDEX IF NOT EXISTS idx_items_status     ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_source     ON items(source_id);
CREATE INDEX IF NOT EXISTS idx_items_updated    ON items(updated_at);
CREATE INDEX IF NOT EXISTS idx_items_canonical  ON items(canonical_id);
"""

# ── 迁移：给已有数据库加新列（幂等）──────────────────────────────────────────

_MIGRATIONS = [
    # items 新增列
    "ALTER TABLE items ADD COLUMN canonical_id INTEGER",
    # users 新增列
    "ALTER TABLE users ADD COLUMN plan TEXT DEFAULT 'free'",
    # 迁移已有付费用户到 pro 套餐
    "UPDATE users SET plan='pro' WHERE is_paid=1 AND (plan IS NULL OR plan='free')",
]


def get_conn() -> sqlite3.Connection:
    """返回 Row-factory 连接，调用方负责 close()。"""
    data_dir = os.path.dirname(DB_PATH)
    if data_dir:
        os.makedirs(data_dir, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """建表建索引（幂等）。"""
    data_dir = os.path.dirname(DB_PATH)
    if data_dir:
        os.makedirs(data_dir, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(_CREATE_SCHEMA)
        # 增量迁移：ALTER TABLE / UPDATE（失败说明已存在，忽略）
        for sql in _MIGRATIONS:
            try:
                conn.execute(sql)
                conn.commit()
            except sqlite3.OperationalError:
                pass
    print(f"[db] 初始化完成: {DB_PATH}")


def _to_json(v) -> str:
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return v or "[]"


def _normalize_title(title: str) -> str:
    """去标点、空格后转小写，用于标题相似度比较。"""
    return re.sub(r'[\s\W]+', '', title or '', flags=re.UNICODE).lower()


def upsert_item_with_version(item: dict) -> bool:
    """插入或更新一条政策条目。

    去重逻辑（按优先级）：
    1. URL 完全相同 → 判断内容是否变化，变化则更新，否则跳过
    2. 归一化标题相同 + 日期相同 → 视为转载重复，标记 canonical_id，返回 False
    - URL 不存在且无重复 → INSERT，返回 True
    - URL 存在且内容变化 → UPDATE + 记录版本，返回 True
    - 重复 → 跳过，返回 False
    """
    url = (item.get("url") or "").strip()
    if not url:
        return False

    data = {
        "url":          url,
        "title":        (item.get("title") or "").strip()[:500],
        "summary":      (item.get("summary") or "")[:2000],
        "content":      (item.get("content") or "")[:10000],
        "date":         item.get("date"),
        "region":       item.get("region"),
        "source_id":    item.get("source_id"),
        "source_name":  item.get("source_name"),
        "categories":   _to_json(item.get("categories", [])),
        "tags":         _to_json(item.get("tags", [])),
        "impact_on":    _to_json(item.get("impact_on", [])),
        "level":        item.get("level", "国家"),
        "status":       item.get("status", "现行"),
        "deadline_date": item.get("deadline_date"),
    }

    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM items WHERE url=?", (url,)).fetchone()

        if row is None:
            # 标题归一化 + 日期相同去重（跨来源转载检测）
            norm = _normalize_title(data["title"])
            date = (data.get("date") or "")[:10]
            if norm and date:
                same_date_rows = conn.execute(
                    "SELECT id, title, canonical_id FROM items WHERE date=? LIMIT 200",
                    (date,),
                ).fetchall()
                for dr in same_date_rows:
                    if _normalize_title(dr["title"]) == norm:
                        # 找到归一化标题相同的条目，标记 canonical_id 后跳过
                        canonical = dr["canonical_id"] or dr["id"]
                        conn.execute(
                            """INSERT INTO items
                               (url,title,summary,content,date,region,source_id,source_name,
                                categories,tags,impact_on,level,status,deadline_date,canonical_id)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (data["url"], data["title"], data["summary"], data["content"],
                             data["date"], data["region"], data["source_id"], data["source_name"],
                             data["categories"], data["tags"], data["impact_on"],
                             data["level"], data["status"], data["deadline_date"], canonical),
                        )
                        conn.commit()
                        return False  # 已作为重复转载处理

            conn.execute(
                """INSERT INTO items
                   (url,title,summary,content,date,region,source_id,source_name,
                    categories,tags,impact_on,level,status,deadline_date)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (data["url"], data["title"], data["summary"], data["content"],
                 data["date"], data["region"], data["source_id"], data["source_name"],
                 data["categories"], data["tags"], data["impact_on"],
                 data["level"], data["status"], data["deadline_date"]),
            )
            conn.commit()
            return True

        # 比较核心字段，判断是否变化
        if ((row["title"] or "").strip() == data["title"] and
                (row["summary"] or "").strip() == data["summary"].strip()):
            return False

        item_id = row["id"]
        conn.execute(
            "INSERT INTO item_versions (item_id, old_data, new_data) VALUES (?,?,?)",
            (item_id,
             json.dumps(dict(row), ensure_ascii=False, default=str),
             json.dumps(data, ensure_ascii=False, default=str)),
        )
        conn.execute(
            """UPDATE items SET
               title=?, summary=?, content=?, date=?, region=?,
               categories=?, tags=?, impact_on=?, level=?, status=?,
               deadline_date=?, updated_at=datetime('now','localtime')
               WHERE id=?""",
            (data["title"], data["summary"], data["content"], data["date"],
             data["region"], data["categories"], data["tags"], data["impact_on"],
             data["level"], data["status"], data["deadline_date"], item_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()
