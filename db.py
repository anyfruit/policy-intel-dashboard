"""db.py — SQLite 数据库层

所有模块的唯一数据库入口。
- get_conn()               → 返回 sqlite3.Connection（调用方负责 close）
- init_db()                → 建表/建索引
- upsert_item_with_version → 插入或更新条目（含版本历史）
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Optional

def _resolve_db_path() -> str:
    path = os.getenv("DB_PATH", "/data/policy.db")
    data_dir = os.path.dirname(path)
    if data_dir:
        try:
            os.makedirs(data_dir, exist_ok=True)
            test = os.path.join(data_dir, ".write_test")
            with open(test, "w") as f:
                f.write("x")
            os.remove(test)
        except (OSError, PermissionError):
            fallback = "/tmp/policy.db"
            print(f"[db] {data_dir} not writable, falling back to {fallback}", flush=True)
            return fallback
    return path

DB_PATH = _resolve_db_path()

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
  read_at           TEXT,
  created_at        TEXT    DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS subscriptions (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id      INTEGER NOT NULL,
  name         TEXT    NOT NULL DEFAULT '我的订阅',
  email        TEXT    DEFAULT '',
  keywords     TEXT    DEFAULT '[]',
  regions      TEXT    DEFAULT '[]',
  categories   TEXT    DEFAULT '[]',
  buckets      TEXT    DEFAULT '[]',
  frequency    TEXT    DEFAULT 'daily',
  webhook_url  TEXT    DEFAULT '',
  active       INTEGER DEFAULT 1,
  created_at   TEXT    DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS user_profiles (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id        INTEGER UNIQUE NOT NULL,
  company_type   TEXT    DEFAULT '',
  provinces      TEXT    DEFAULT '[]',
  business_stage TEXT    DEFAULT '全阶段',
  updated_at     TEXT    DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_items_date     ON items(date);
CREATE INDEX IF NOT EXISTS idx_items_region   ON items(region);
CREATE INDEX IF NOT EXISTS idx_items_level    ON items(level);
CREATE INDEX IF NOT EXISTS idx_items_status   ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_source   ON items(source_id);
CREATE INDEX IF NOT EXISTS idx_items_updated  ON items(updated_at);
"""


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
        # 迁移：补充旧数据库中可能缺失的 is_paid 列
        try:
            conn.execute("ALTER TABLE users ADD COLUMN is_paid INTEGER DEFAULT 0")
            conn.commit()
        except Exception:
            pass  # 列已存在，忽略
        # 迁移：补充旧数据库中可能缺失的 read_at 列
        try:
            conn.execute("ALTER TABLE notifications ADD COLUMN read_at TEXT")
            conn.commit()
        except Exception:
            pass  # 列已存在，忽略
        # 迁移：补充旧数据库中可能缺失的 subscriptions 表
        try:
            conn.executescript("""
CREATE TABLE IF NOT EXISTS subscriptions (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id      INTEGER NOT NULL,
  name         TEXT    NOT NULL DEFAULT '我的订阅',
  email        TEXT    DEFAULT '',
  keywords     TEXT    DEFAULT '[]',
  regions      TEXT    DEFAULT '[]',
  categories   TEXT    DEFAULT '[]',
  buckets      TEXT    DEFAULT '[]',
  frequency    TEXT    DEFAULT 'daily',
  webhook_url  TEXT    DEFAULT '',
  active       INTEGER DEFAULT 1,
  created_at   TEXT    DEFAULT (datetime('now','localtime'))
);
""")
            conn.commit()
        except Exception:
            pass
    print(f"[db] 初始化完成: {DB_PATH}")


def _to_json(v) -> str:
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return v or "[]"


def upsert_item_with_version(item: dict) -> bool:
    """插入或更新一条政策条目。

    - URL 不存在 → INSERT，返回 True
    - URL 存在且内容变化 → UPDATE + 记录版本，返回 True
    - URL 存在且内容相同 → 跳过，返回 False
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
