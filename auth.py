"""auth.py — 超轻量本地认证（cookie token）

目标：
- 给 policy-intel-dashboard/server.py 提供最小可用的注册/登录/鉴权能力
- 不依赖额外第三方库

安全说明：
- 这是本地单机 demo 的实现，不等同生产级账号系统。
- token 使用 HMAC 签名 + exp 过期，存放于 HttpOnly cookie。

"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from fastapi import Cookie, Depends, HTTPException, status

import sys
sys.path.insert(0, str(Path(__file__).parent))
from db import get_conn


_SECRET = (os.environ.get("POLICY_DASH_SECRET") or os.environ.get("SECRET_KEY") or "policy-dash-dev-secret").encode("utf-8")
_TOKEN_TTL_SEC = int(os.environ.get("POLICY_DASH_TOKEN_TTL_SEC") or 86400 * 7)


def init_users_table() -> None:
    conn = get_conn()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              username TEXT UNIQUE NOT NULL,
              password_hash TEXT NOT NULL,
              salt TEXT NOT NULL,
              email TEXT DEFAULT '',
              created_at TEXT DEFAULT (datetime('now','localtime'))
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def _pbkdf2(password: str, salt: str) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return base64.b64encode(dk).decode("utf-8")


def create_user(username: str, password: str) -> dict:
    u = (username or "").strip()
    p = (password or "")
    if len(u) < 2:
        raise ValueError("用户名至少2位")
    if len(p) < 6:
        raise ValueError("密码至少6位")

    salt = base64.b64encode(os.urandom(16)).decode("utf-8")
    ph = _pbkdf2(p, salt)

    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO users (username,password_hash,salt) VALUES (?,?,?)",
            (u, ph, salt),
        )
        conn.commit()
        row = conn.execute("SELECT id,username,created_at FROM users WHERE username=?", (u,)).fetchone()
        return dict(row) if row else {"username": u}
    except Exception as e:
        # UNIQUE constraint
        if "UNIQUE" in str(e).upper():
            raise ValueError("用户名已存在")
        raise
    finally:
        conn.close()


def authenticate_user(username: str, password: str) -> Optional[dict]:
    u = (username or "").strip()
    p = (password or "")
    if not u or not p:
        return None
    conn = get_conn()
    try:
        row = conn.execute("SELECT id,username,password_hash,salt FROM users WHERE username=?", (u,)).fetchone()
        if not row:
            return None
        ph = _pbkdf2(p, row["salt"])
        if hmac.compare_digest(ph, row["password_hash"]):
            return {"id": row["id"], "username": row["username"]}
        return None
    finally:
        conn.close()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode((s + pad).encode("utf-8"))


def create_access_token(user_id: int, username: str) -> str:
    payload = {
        "uid": int(user_id),
        "u": (username or "").strip(),
        "exp": int(time.time()) + _TOKEN_TTL_SEC,
    }
    body = _b64url(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    sig = _b64url(hmac.new(_SECRET, body.encode("utf-8"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def _verify_token(token: str) -> Optional[dict]:
    if not token or "." not in token:
        return None
    body, sig = token.split(".", 1)
    exp_sig = _b64url(hmac.new(_SECRET, body.encode("utf-8"), hashlib.sha256).digest())
    if not hmac.compare_digest(exp_sig, sig):
        return None
    try:
        payload = json.loads(_b64url_decode(body).decode("utf-8"))
    except Exception:
        return None
    if int(payload.get("exp") or 0) < int(time.time()):
        return None
    if not payload.get("uid"):
        return None
    return payload


def get_current_user(access_token: Optional[str] = Cookie(default=None)) -> dict:
    payload = _verify_token(access_token or "")
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")
    return {"id": int(payload["uid"]), "username": payload.get("u") or ""}


def get_optional_user(access_token: Optional[str] = Cookie(default=None)) -> Optional[dict]:
    payload = _verify_token(access_token or "")
    if not payload:
        return None
    return {"id": int(payload["uid"]), "username": payload.get("u") or ""}
