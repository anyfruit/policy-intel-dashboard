"""server.py — 储能政策情报看板 Web 服务

Fly.io 原版前端（React SPA）+ 全量 JSON API
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import Cookie, Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import auth
import db
from db import get_conn
from tags import CATEGORY_TYPES
from impact_score import calculate_impact_score, COMPANY_TYPES, BUSINESS_STAGES, PROVINCES_LIST

app = FastAPI(title="储能政策情报看板", docs_url=None, redoc_url=None)

BASE_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# ── 依赖 ─────────────────────────────────────────────────────────────────────

def _get_user_with_plan(access_token: Optional[str] = Cookie(default=None)) -> Optional[dict]:
    payload = auth._verify_token(access_token or "")
    if not payload:
        return None
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, username, email, is_paid FROM users WHERE id=?",
            (int(payload["uid"]),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def require_login(user=Depends(_get_user_with_plan)) -> dict:
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


def require_paid(user=Depends(_get_user_with_plan)) -> dict:
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    if not user.get("is_paid"):
        raise HTTPException(status_code=402, detail="此功能需要付费订阅")
    return user


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def _parse_json_field(v) -> list:
    if isinstance(v, list):
        return v
    try:
        result = json.loads(v or "[]")
        return result if isinstance(result, list) else []
    except Exception:
        return []


def _item_to_dict(row) -> dict:
    d = dict(row)
    for f in ("categories", "tags", "impact_on"):
        d[f] = _parse_json_field(d.get(f))
    return d


def _query_items(
    q: str = "",
    region: str = "",
    level: str = "",
    status: str = "",
    category: str = "",
    source: str = "",
    tags: str = "",
    page: int = 1,
    limit: int = 20,
) -> tuple[list[dict], int]:
    conditions = []
    params: list = []

    if q:
        conditions.append("(title LIKE ? OR summary LIKE ? OR content LIKE ?)")
        like = f"%{q}%"
        params += [like, like, like]
    if region:
        conditions.append("region = ?")
        params.append(region)
    if level:
        conditions.append("level = ?")
        params.append(level)
    if status:
        conditions.append("status = ?")
        params.append(status)
    if category:
        conditions.append("categories LIKE ?")
        params.append(f"%{category}%")
    if source:
        conditions.append("source_name LIKE ?")
        params.append(f"%{source}%")
    if tags:
        conditions.append("tags LIKE ?")
        params.append(f"%{tags}%")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    offset = (max(page, 1) - 1) * limit

    conn = get_conn()
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM items {where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM items {where} ORDER BY date DESC, id DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        return [_item_to_dict(r) for r in rows], total
    finally:
        conn.close()


def _get_regions() -> list[str]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT DISTINCT region FROM items WHERE region IS NOT NULL AND region != '' ORDER BY region"
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def _get_stats() -> dict:
    conn = get_conn()
    try:
        total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        by_level = conn.execute(
            "SELECT level, COUNT(*) as cnt FROM items GROUP BY level"
        ).fetchall()
        by_status = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM items GROUP BY status"
        ).fetchall()
        recent_30d = conn.execute(
            "SELECT COUNT(*) FROM items WHERE date >= date('now','-30 days')"
        ).fetchone()[0]
        recent_7d = conn.execute(
            "SELECT COUNT(*) FROM items WHERE date >= date('now','-7 days')"
        ).fetchone()[0]
        by_source = conn.execute(
            "SELECT source_name, COUNT(*) as cnt FROM items WHERE source_name IS NOT NULL GROUP BY source_name ORDER BY cnt DESC"
        ).fetchall()
        return {
            "total": total,
            "recent_30d": recent_30d,
            "recent_7d": recent_7d,
            "by_level": {r["level"]: r["cnt"] for r in by_level},
            "by_status": {r["status"]: r["cnt"] for r in by_status},
            "by_source": [{"name": r["source_name"], "count": r["cnt"]} for r in by_source],
        }
    finally:
        conn.close()


def _classify_bucket(categories: list) -> str:
    """将 categories 映射到 bucket 标签。"""
    for cat in categories:
        if "补贴" in cat or "激励" in cat:
            return "补贴"
        if "招标" in cat or "采购" in cat or "竞配" in cat:
            return "招标"
        if "合规" in cat or "标准" in cat or "规范" in cat:
            return "合规"
    return "政策"


def _get_user_profile(user_id: int) -> dict:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT company_type, provinces, business_stage FROM user_profiles WHERE user_id=?",
            (user_id,),
        ).fetchone()
        if not row:
            return {}
        p = dict(row)
        if isinstance(p.get("provinces"), str):
            try:
                p["provinces"] = json.loads(p["provinces"])
            except Exception:
                p["provinces"] = []
        return p
    finally:
        conn.close()


# ── Startup ───────────────────────────────────────────────────────────────────

def _seed_from_backup() -> None:
    import sqlite3 as _sqlite3
    seed_path = BASE_DIR / "seed.db"
    if not seed_path.exists():
        return
    conn = get_conn()
    try:
        count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    finally:
        conn.close()
    if count > 0:
        return
    seed_conn = _sqlite3.connect(str(seed_path))
    seed_conn.row_factory = _sqlite3.Row
    try:
        rows = seed_conn.execute("SELECT * FROM items").fetchall()
        conn = get_conn()
        try:
            for row in rows:
                d = dict(row)
                conn.execute(
                    """INSERT OR IGNORE INTO items
                       (url,title,summary,content,date,region,source_id,source_name,
                        categories,tags,impact_on,level,status,deadline_date,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (d.get("url"), d.get("title"), d.get("summary"), d.get("content"),
                     d.get("date"), d.get("region"), d.get("source_id"), d.get("source_name"),
                     d.get("categories", "[]"), d.get("tags", "[]"), d.get("impact_on", "[]"),
                     d.get("level", "国家"), d.get("status", "现行"), d.get("deadline_date"),
                     d.get("created_at"), d.get("updated_at")),
                )
            conn.commit()
            print(f"[seed] 从 seed.db 导入 {len(rows)} 条政策数据")
        finally:
            conn.close()
    finally:
        seed_conn.close()


logger = logging.getLogger("scheduler")


def _match_subscription(sub: dict, item: dict) -> bool:
    """检查一条政策是否匹配订阅规则（关键词 + 地区）。"""
    # 地区过滤：订阅指定了地区，但政策地区不在其中 → 不匹配
    regions = sub.get("regions") or []
    if regions:
        item_region = item.get("region") or ""
        if not any(r in item_region or item_region in r for r in regions):
            return False

    # 分类过滤
    categories = sub.get("categories") or []
    if categories:
        item_cats = json.loads(item.get("categories") or "[]") if isinstance(item.get("categories"), str) else (item.get("categories") or [])
        if not any(c in item_cats for c in categories):
            return False

    # 关键词过滤：订阅指定了关键词，政策标题/摘要/内容中至少含一个 → 匹配
    keywords = sub.get("keywords") or []
    if keywords:
        text = " ".join([
            item.get("title") or "",
            item.get("summary") or "",
            item.get("content") or "",
        ]).lower()
        if not any(kw.lower() in text for kw in keywords if kw):
            return False

    return True


def _dispatch_notifications(new_items: list) -> None:
    """爬虫完成后，将新政策与订阅规则匹配，发送邮件或存站内通知。"""
    if not new_items:
        return

    import notifier

    conn = get_conn()
    try:
        subs = conn.execute(
            "SELECT s.*, u.email AS user_email FROM subscriptions s JOIN users u ON s.user_id=u.id WHERE s.active=1"
        ).fetchall()

        for sub_row in subs:
            sub = {
                "id": sub_row["id"],
                "user_id": sub_row["user_id"],
                "name": sub_row["name"],
                "email": sub_row["email"] or sub_row["user_email"] or "",
                "keywords": json.loads(sub_row["keywords"] or "[]"),
                "regions": json.loads(sub_row["regions"] or "[]"),
                "categories": json.loads(sub_row["categories"] or "[]"),
                "frequency": sub_row["frequency"],
            }

            matched = [it for it in new_items if _match_subscription(sub, it)]
            if not matched:
                continue

            # 存站内通知（无论是否有 SMTP）
            for it in matched:
                try:
                    conn.execute(
                        "INSERT INTO notifications (user_id, item_id, subscription_name) VALUES (?,?,?)",
                        (sub["user_id"], it["id"], sub["name"]),
                    )
                except Exception:
                    pass
            conn.commit()

            # 即时订阅 + 有邮箱 + SMTP 已配置 → 发邮件
            if sub["frequency"] == "instant" and sub["email"] and notifier.smtp_configured():
                try:
                    html = notifier.build_digest_html(
                        [{"subscription_name": sub["name"], "items": matched}],
                        period_label="即时推送",
                    )
                    notifier.send_email(sub["email"], f"【政策预警】{sub['name']} — {len(matched)} 条新政策", html)
                    logger.info("📧 邮件已发送 → %s（%d 条）", sub["email"], len(matched))
                except Exception:
                    logger.exception("❌ 邮件发送失败 sub_id=%s email=%s", sub["id"], sub["email"])
    finally:
        conn.close()


def _run_daily_scrape():
    """后台线程：每 24 小时自动抓取一次政策数据。
    首次运行延迟 5 分钟，避免影响服务启动。

    依赖环境变量：
      BRAVE_API_KEY — 省级政策 Brave Search 搜索，未设置则只跑月报。
                      请在 Railway → Variables 里添加此变量。
    """
    # 首次延迟 5 分钟
    time.sleep(5 * 60)

    while True:
        start = datetime.now()
        logger.info("🤖 定时爬虫启动 %s", start.strftime("%Y-%m-%d %H:%M:%S"))
        try:
            import auto_scrape
            auto_scrape.run_monthly()
            brave_key = os.environ.get("BRAVE_API_KEY", "")
            if brave_key:
                auto_scrape.run_province_search()
            else:
                logger.warning("BRAVE_API_KEY 未设置，省份搜索已跳过（仅跑月报）")
            auto_scrape.run_deadline_extraction()
            auto_scrape.run_cleanup()
            elapsed = (datetime.now() - start).total_seconds()
            logger.info("✅ 定时爬虫完成，耗时 %.0fs", elapsed)

            # 查询今日新增政策，触发订阅通知
            try:
                today = start.strftime("%Y-%m-%d")
                conn = get_conn()
                try:
                    rows = conn.execute(
                        "SELECT * FROM items WHERE date(created_at)=? OR date(updated_at)=?",
                        (today, today),
                    ).fetchall()
                    new_items = [dict(r) for r in rows]
                finally:
                    conn.close()
                _dispatch_notifications(new_items)
                logger.info("🔔 通知派发完成（%d 条新政策）", len(new_items))
            except Exception:
                logger.exception("❌ 通知派发异常")
        except Exception:
            logger.exception("❌ 定时爬虫异常，下次仍会重试")

        # 等待 24 小时后再次运行
        time.sleep(24 * 60 * 60)


@app.on_event("startup")
async def startup():
    db.init_db()
    auth.init_users_table()
    _seed_from_backup()

    # 启动后台定时爬虫线程（daemon=True：主进程退出时自动结束）
    t = threading.Thread(target=_run_daily_scrape, name="daily-scraper", daemon=True)
    t.start()
    logger.info("后台定时爬虫已启动（5 分钟后首次运行，之后每 24 小时一次）")


# ── 主页：Fly.io React SPA ────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = BASE_DIR / "templates" / "app.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


# ── JSON Auth API ──────────────────────────────────────────────────────────────

@app.get("/api/auth/me")
async def api_auth_me(user=Depends(_get_user_with_plan)):
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return {
        "id": user["id"],
        "username": user["username"],
        "email": user.get("email") or "",
        "is_paid": bool(user.get("is_paid")),
        "smtp_configured": False,
    }


@app.post("/api/auth/login")
async def api_auth_login(request: Request):
    body = await request.json()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    u = auth.authenticate_user(username, password)
    if not u:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = auth.create_access_token(u["id"], u["username"])
    resp = JSONResponse({
        "id": u["id"],
        "username": u["username"],
        "is_paid": bool(u.get("is_paid")),
    })
    resp.set_cookie("access_token", token, httponly=True, max_age=auth._TOKEN_TTL_SEC, samesite="lax")
    return resp


@app.post("/api/auth/register")
async def api_auth_register(request: Request):
    body = await request.json()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    try:
        u = auth.create_user(username, password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    token = auth.create_access_token(u["id"], u["username"])
    resp = JSONResponse({
        "id": u["id"],
        "username": u["username"],
        "is_paid": False,
    })
    resp.set_cookie("access_token", token, httponly=True, max_age=auth._TOKEN_TTL_SEC, samesite="lax")
    return resp


@app.post("/api/auth/logout")
async def api_auth_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("access_token")
    return resp


# ── Public API（无需登录）─────────────────────────────────────────────────────

@app.get("/api/public/dashboard")
async def api_public_dashboard(latest_n: int = 10):
    conn = get_conn()
    try:
        total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        recent_7d = conn.execute(
            "SELECT COUNT(*) FROM items WHERE date >= date('now','-7 days')"
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT id, title, url, date, source_name, region, categories FROM items ORDER BY date DESC, id DESC LIMIT ?",
            (latest_n,),
        ).fetchall()
        latest = []
        for r in rows:
            cats = _parse_json_field(r["categories"])
            latest.append({
                "id": r["id"],
                "title": r["title"],
                "url": r["url"] or "",
                "date": r["date"],
                "source_name": r["source_name"] or "",
                "region": r["region"] or "全国",
                "bucket": _classify_bucket(cats),
            })
        updated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        return {
            "total": total,
            "recent_7d": recent_7d,
            "updated_at": updated_at,
            "latest": latest,
        }
    finally:
        conn.close()


@app.get("/api/public/items")
async def api_public_items(limit: int = 10):
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, title, url, date, summary, source_name, region, level, status, categories, tags FROM items ORDER BY date DESC, id DESC LIMIT ?",
            (min(limit, 20),),
        ).fetchall()
        items = []
        for r in rows:
            items.append({
                "id": r["id"],
                "title": r["title"],
                "url": r["url"] or "",
                "date": r["date"],
                "doc_no": None,
                "summary": r["summary"] or "",
                "source_name": r["source_name"] or "",
                "region": r["region"] or "全国",
                "level": r["level"] or "",
                "status": r["status"] or "",
                "version_count": 1,
                "categories": _parse_json_field(r["categories"]),
                "tags": _parse_json_field(r["tags"]),
            })
        return {"items": items}
    finally:
        conn.close()


# ── Stats API ──────────────────────────────────────────────────────────────────

@app.get("/api/stats")
async def api_stats():
    return _get_stats()


@app.get("/api/stats/trend")
async def api_stats_trend(months: int = 12):
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT strftime('%Y-%m', date) as mm,
                      SUM(CASE WHEN categories LIKE '%补贴%' THEN 1 ELSE 0 END) as subsidy,
                      SUM(CASE WHEN categories LIKE '%招标%' THEN 1 ELSE 0 END) as tender,
                      SUM(CASE WHEN categories LIKE '%合规%' OR categories LIKE '%标准%' THEN 1 ELSE 0 END) as compliance,
                      COUNT(*) as policy
               FROM items
               WHERE date IS NOT NULL AND date >= date('now', ? || ' months')
               GROUP BY mm ORDER BY mm""",
            (f"-{months}",),
        ).fetchall()
        series = {"政策": [], "补贴": [], "招标": [], "合规": []}
        months_list = []
        for r in rows:
            months_list.append(r["mm"])
            series["政策"].append(r["policy"])
            series["补贴"].append(r["subsidy"])
            series["招标"].append(r["tender"])
            series["合规"].append(r["compliance"])
        return {"months": months_list, "series": series}
    finally:
        conn.close()


# ── Filters & Sources API ─────────────────────────────────────────────────────

@app.get("/api/filters/meta")
async def api_filters_meta(days: int = 0):
    conn = get_conn()
    try:
        date_filter = f"WHERE date >= date('now', '-{days} days')" if days > 0 else ""
        regions = conn.execute(
            f"SELECT region, COUNT(*) as cnt FROM items {date_filter} GROUP BY region ORDER BY cnt DESC"
        ).fetchall()
        cats = conn.execute(
            f"SELECT categories FROM items {date_filter}"
        ).fetchall()
        statuses = conn.execute(
            f"SELECT status, COUNT(*) as cnt FROM items {date_filter} GROUP BY status"
        ).fetchall()

        region_counts = {r["region"]: r["cnt"] for r in regions if r["region"]}
        region_list = [r["region"] for r in regions if r["region"]]

        cat_counts: dict = {}
        for row in cats:
            for c in _parse_json_field(row["categories"]):
                cat_counts[c] = cat_counts.get(c, 0) + 1

        status_counts = {r["status"]: r["cnt"] for r in statuses if r["status"]}

        return {
            "regions": region_list,
            "region_counts": region_counts,
            "category_counts": cat_counts,
            "status_counts": status_counts,
            "levels": ["国家", "省", "市", "行业"],
            "categories": CATEGORY_TYPES,
        }
    finally:
        conn.close()


@app.get("/api/sources")
async def api_sources():
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT source_name, COUNT(*) as cnt FROM items WHERE source_name IS NOT NULL GROUP BY source_name ORDER BY cnt DESC LIMIT 20"
        ).fetchall()
        return [{"name": r["source_name"], "count": r["cnt"], "status": "ok"} for r in rows]
    finally:
        conn.close()


# ── Dashboard API（需登录）────────────────────────────────────────────────────

@app.get("/api/dashboard")
async def api_dashboard(latest_n: int = 12, user=Depends(require_login)):
    conn = get_conn()
    try:
        # 最新条目
        rows = conn.execute(
            "SELECT id, title, url, date, source_name, region, categories FROM items ORDER BY date DESC, id DESC LIMIT ?",
            (latest_n,),
        ).fetchall()
        latest = []
        for r in rows:
            cats = _parse_json_field(r["categories"])
            latest.append({
                "id": r["id"],
                "title": r["title"],
                "url": r["url"] or "",
                "date": r["date"],
                "source_name": r["source_name"] or "",
                "region": r["region"] or "全国",
                "bucket": _classify_bucket(cats),
            })

        # 省份矩阵
        region_rows = conn.execute(
            """SELECT region,
                      SUM(CASE WHEN categories LIKE '%补贴%' THEN 1 ELSE 0 END) as subsidy,
                      SUM(CASE WHEN categories LIKE '%招标%' THEN 1 ELSE 0 END) as tender,
                      SUM(CASE WHEN categories LIKE '%合规%' OR categories LIKE '%标准%' THEN 1 ELSE 0 END) as compliance,
                      COUNT(*) as total
               FROM items WHERE region IS NOT NULL AND region != ''
               GROUP BY region ORDER BY total DESC LIMIT 30"""
        ).fetchall()
        matrix = []
        for r in region_rows:
            total = r["total"]
            subsidy = r["subsidy"]
            tender = r["tender"]
            compliance = r["compliance"]
            policy = total - subsidy - tender - compliance
            matrix.append({
                "region": r["region"],
                "policy": max(0, policy),
                "subsidy": subsidy,
                "tender": tender,
                "compliance": compliance,
                "total": total,
            })

        updated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        return {"matrix": matrix, "latest": latest, "updated_at": updated_at}
    finally:
        conn.close()


@app.get("/api/dashboard/trends")
async def api_dashboard_trends(user=Depends(require_login)):
    conn = get_conn()
    try:
        # Top keywords from tags
        rows = conn.execute(
            "SELECT tags FROM items WHERE tags IS NOT NULL AND tags != '[]' ORDER BY date DESC LIMIT 200"
        ).fetchall()
        kw_counts: dict = {}
        for r in rows:
            for t in _parse_json_field(r["tags"]):
                kw_counts[t] = kw_counts.get(t, 0) + 1
        top_kw = sorted(kw_counts.items(), key=lambda x: -x[1])[:20]
        return {
            "top_keywords": [{"word": k, "count": v} for k, v in top_kw],
        }
    finally:
        conn.close()


@app.get("/api/dashboard/intel")
async def api_dashboard_intel(user=Depends(require_login)):
    conn = get_conn()
    try:
        # 解读覆盖率（item_analyses 表中已有解读的条目数）
        total_count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        analyzed_count = conn.execute(
            "SELECT COUNT(*) FROM item_analyses WHERE analysis_data IS NOT NULL AND analysis_data != ''"
        ).fetchone()[0]
        pct = round(analyzed_count * 100 / total_count) if total_count > 0 else 0

        # 本周新增 + 分类统计
        week_rows = conn.execute(
            "SELECT id, title, url, date, source_name, region, categories FROM items WHERE date >= date('now','-7 days') ORDER BY date DESC LIMIT 50"
        ).fetchall()
        week_buckets = {"policy": 0, "subsidy": 0, "tender": 0, "compliance": 0}
        for r in week_rows:
            cats = _parse_json_field(r["categories"])
            b = _classify_bucket(cats)
            if b in week_buckets:
                week_buckets[b] += 1
            else:
                week_buckets["policy"] += 1
        week_new = {
            "total": len(week_rows),
            "policy": week_buckets["policy"],
            "subsidy": week_buckets["subsidy"],
            "tender": week_buckets["tender"],
            "compliance": week_buckets["compliance"],
        }

        # 机会雷达（近30天，含"补贴"/"奖励"/"支持"类关键词）
        opp_kw = ["补贴", "奖励", "扶持", "支持", "激励", "资金", "申报"]
        opp_conditions = " OR ".join(f"title LIKE '%{k}%'" for k in opp_kw)
        opp_rows = conn.execute(
            f"SELECT id, title, url, date FROM items WHERE date >= date('now','-30 days') AND ({opp_conditions}) ORDER BY date DESC LIMIT 5"
        ).fetchall()
        opp_items = [{"id": r["id"], "title": r["title"], "url": r["url"] or "", "date": r["date"]} for r in opp_rows]
        opportunity = {"count": len(opp_items), "keywords": opp_kw[:5], "items": opp_items}

        # 风险预警（近30天，含"处罚"/"违规"/"整改"类关键词）
        risk_kw = ["处罚", "违规", "整改", "禁止", "停产", "吊销", "罚款"]
        risk_conditions = " OR ".join(f"title LIKE '%{k}%'" for k in risk_kw)
        risk_rows = conn.execute(
            f"SELECT id, title, url, date FROM items WHERE date >= date('now','-30 days') AND ({risk_conditions}) ORDER BY date DESC LIMIT 5"
        ).fetchall()
        risk_items = [{"id": r["id"], "title": r["title"], "url": r["url"] or "", "date": r["date"]} for r in risk_rows]
        risk = {"count": len(risk_items), "keywords": risk_kw[:5], "items": risk_items}

        return {
            "coverage": {"pct": pct, "analyzed": analyzed_count, "total": total_count},
            "opportunity": opportunity,
            "risk": risk,
            "week_new": week_new,
        }
    finally:
        conn.close()


# ── Items API ──────────────────────────────────────────────────────────────────

@app.get("/api/items")
async def api_items(
    q: str = "",
    region: str = "",
    level: str = "",
    status: str = "",
    category: str = "",
    source: str = "",
    tags: str = "",
    page: int = 1,
    limit: int = 20,
    user=Depends(_get_user_with_plan),
):
    limit = min(limit, 100)
    items, total = _query_items(q, region, level, status, category, source, tags, page, limit)
    # Add version_count and bucket fields
    for item in items:
        item.setdefault("version_count", 1)
        item["bucket"] = _classify_bucket(item.get("categories", []))
    return {"items": items, "total": total, "page": page}


@app.get("/api/items/{item_id}")
async def api_item(item_id: int):
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404)
    item = _item_to_dict(row)
    item.setdefault("version_count", 1)
    item["bucket"] = _classify_bucket(item.get("categories", []))
    return item


@app.get("/api/items/{item_id}/versions")
async def api_item_versions(item_id: int, user=Depends(require_login)):
    return {"versions": []}


@app.get("/api/items/{item_id}/diff")
async def api_item_diff(item_id: int, user=Depends(require_login)):
    return {"diff": None}


@app.get("/api/items/{item_id}/checklist")
async def api_item_checklist(item_id: int, format: str = "md", user=Depends(require_login)):
    conn = get_conn()
    try:
        row = conn.execute("SELECT title FROM items WHERE id=?", (item_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404)
    return JSONResponse({"title": row["title"], "checklist": []})


@app.get("/api/items/{item_id}/analysis")
async def api_item_analysis(item_id: int, mode: str = "standard", force: bool = False, user=Depends(require_login)):
    """政策 AI 解读（与 ai/interpret 共用逻辑）。"""
    # 检查缓存
    if not force:
        conn = get_conn()
        try:
            cached = conn.execute(
                "SELECT analysis_data FROM item_analyses WHERE item_id=?", (item_id,)
            ).fetchone()
            if cached:
                return json.loads(cached["analysis_data"])
        finally:
            conn.close()

    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404)

    item = _item_to_dict(row)
    return {
        "item_id": item_id,
        "title": item.get("title", ""),
        "core_content": "政策解读功能需要 OPENAI_API_KEY。",
        "key_requirements": [],
        "impact_analysis": {"developers": "", "manufacturers": "", "grid": ""},
        "action_items": [],
        "timeline": "",
        "overall_assessment": "",
        "status": "no_api_key",
    }


# ── Search API ────────────────────────────────────────────────────────────────

@app.get("/api/search")
async def api_search(q: str = "", limit: int = 20, user=Depends(_get_user_with_plan)):
    if not q.strip():
        return {"items": [], "total": 0}
    items, total = _query_items(q=q, page=1, limit=min(limit, 50))
    for item in items:
        item.setdefault("version_count", 1)
        item["bucket"] = _classify_bucket(item.get("categories", []))
    return {"items": items, "total": total, "query": q}


# ── Compare API ───────────────────────────────────────────────────────────────

@app.post("/api/compare")
async def api_compare(request: Request, user=Depends(require_login)):
    body = await request.json()
    ids = body.get("ids", [])
    if not ids:
        raise HTTPException(400, "请提供政策 ID 列表")
    conn = get_conn()
    try:
        items = []
        for item_id in ids[:4]:
            row = conn.execute("SELECT * FROM items WHERE id=?", (int(item_id),)).fetchone()
            if row:
                items.append(_item_to_dict(row))
        return {"items": items}
    finally:
        conn.close()


# ── Province Compare API ───────────────────────────────────────────────────────

@app.get("/api/compare-provinces")
async def api_compare_provinces(provinces: str = "", category: str = "", user=Depends(require_login)):
    """
    Compare policies across provinces.
    provinces: comma-separated province names
    category: optional category filter substring
    Returns per-province stats: total, recent_30d, top_categories, latest_3
    """
    prov_list = [p.strip() for p in provinces.split(",") if p.strip()]
    if not prov_list:
        raise HTTPException(400, "请提供省份列表（逗号分隔）")
    if len(prov_list) > 4:
        prov_list = prov_list[:4]

    conn = get_conn()
    try:
        import json as _json
        from datetime import date, timedelta
        cutoff = (date.today() - timedelta(days=30)).isoformat()
        result = []
        for prov in prov_list:
            if category:
                total_row = conn.execute(
                    "SELECT COUNT(*) FROM items WHERE region=? AND categories LIKE ?",
                    (prov, f"%{category}%")
                ).fetchone()
                recent_row = conn.execute(
                    "SELECT COUNT(*) FROM items WHERE region=? AND categories LIKE ? AND date>=?",
                    (prov, f"%{category}%", cutoff)
                ).fetchone()
                cat_rows = conn.execute(
                    "SELECT categories FROM items WHERE region=? AND categories LIKE ? AND categories IS NOT NULL AND categories!=''",
                    (prov, f"%{category}%")
                ).fetchall()
                latest_rows = conn.execute(
                    "SELECT id,title,date,categories,summary_1line FROM items WHERE region=? AND categories LIKE ? ORDER BY date DESC LIMIT 3",
                    (prov, f"%{category}%")
                ).fetchall()
            else:
                total_row = conn.execute(
                    "SELECT COUNT(*) FROM items WHERE region=?", (prov,)
                ).fetchone()
                recent_row = conn.execute(
                    "SELECT COUNT(*) FROM items WHERE region=? AND date>=?", (prov, cutoff)
                ).fetchone()
                cat_rows = conn.execute(
                    "SELECT categories FROM items WHERE region=? AND categories IS NOT NULL AND categories!=''",
                    (prov,)
                ).fetchall()
                latest_rows = conn.execute(
                    "SELECT id,title,date,categories,summary_1line FROM items WHERE region=? ORDER BY date DESC LIMIT 3",
                    (prov,)
                ).fetchall()

            # tally categories (properly expands JSON arrays)
            cat_counts: dict = {}
            for (cats_json,) in cat_rows:
                try:
                    cats = _json.loads(cats_json) if cats_json else []
                except Exception:
                    cats = [cats_json] if cats_json else []
                for c in cats:
                    c = c.strip()
                    if c:
                        cat_counts[c] = cat_counts.get(c, 0) + 1
            top_cats = sorted(cat_counts.items(), key=lambda x: -x[1])[:5]

            latest = []
            for row in latest_rows:
                latest.append({
                    "id": row[0],
                    "title": row[1],
                    "date": row[2],
                    "categories": row[3],
                    "summary_1line": row[4],
                })

            result.append({
                "province": prov,
                "total": total_row[0] if total_row else 0,
                "recent_30d": recent_row[0] if recent_row else 0,
                "top_categories": [{"name": k, "count": v} for k, v in top_cats],
                "latest_3": latest,
            })
        return {"provinces": result}
    finally:
        conn.close()


# ── Bookmarks API（stub）─────────────────────────────────────────────────────

@app.get("/api/bookmarks")
async def api_bookmarks(user=Depends(require_login)):
    return {"items": []}


@app.get("/api/bookmarks/{item_id}/status")
async def api_bookmark_status(item_id: int, user=Depends(require_login)):
    return {"bookmarked": False}


@app.post("/api/bookmarks/{item_id}")
async def api_bookmark_add(item_id: int, user=Depends(require_login)):
    return {"ok": True, "bookmarked": True}


@app.delete("/api/bookmarks/{item_id}")
async def api_bookmark_delete(item_id: int, user=Depends(require_login)):
    return {"ok": True, "bookmarked": False}


# ── Notifications API ─────────────────────────────────────────────────────────

@app.get("/api/notifications/count")
async def api_notif_count(user=Depends(require_login)):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM notifications WHERE user_id=? AND read_at IS NULL",
            (user["id"],),
        ).fetchone()
        return {"unread": row["cnt"] if row else 0}
    except Exception:
        return {"unread": 0}
    finally:
        conn.close()


@app.get("/api/notifications")
async def api_notifications(unread_only: bool = False, limit: int = 20, offset: int = 0, user=Depends(require_login)):
    conn = get_conn()
    try:
        where = "n.user_id=?"
        params: list = [user["id"]]
        if unread_only:
            where += " AND n.read_at IS NULL"
        total = conn.execute(f"SELECT COUNT(*) as cnt FROM notifications n WHERE {where}", params).fetchone()["cnt"]
        rows = conn.execute(
            f"""SELECT n.id, n.item_id, n.subscription_name, n.created_at, n.read_at,
                       i.title, i.url, i.region, i.date
                FROM notifications n LEFT JOIN items i ON n.item_id=i.id
                WHERE {where} ORDER BY n.created_at DESC LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()
        items = [dict(r) for r in rows]
        return {"items": items, "total": total}
    except Exception:
        return {"items": [], "total": 0}
    finally:
        conn.close()


@app.post("/api/notifications/{notif_id}/read")
async def api_notif_read(notif_id: int, user=Depends(require_login)):
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE notifications SET read_at=datetime('now','localtime') WHERE id=? AND user_id=?",
            (notif_id, user["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@app.post("/api/notifications/read-all")
async def api_notif_read_all(user=Depends(require_login)):
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE notifications SET read_at=datetime('now','localtime') WHERE user_id=? AND read_at IS NULL",
            (user["id"],),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


# ── Subscriptions API ─────────────────────────────────────────────────────────

@app.get("/api/subscriptions")
async def api_subscriptions(user=Depends(require_login)):
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM subscriptions WHERE user_id=? AND active=1 ORDER BY created_at DESC",
            (user["id"],),
        ).fetchall()
        result = []
        for r in rows:
            result.append({
                "id": r["id"],
                "name": r["name"],
                "email": r["email"],
                "keywords": json.loads(r["keywords"] or "[]"),
                "regions": json.loads(r["regions"] or "[]"),
                "categories": json.loads(r["categories"] or "[]"),
                "buckets": json.loads(r["buckets"] or "[]"),
                "frequency": r["frequency"],
                "webhook_url": r["webhook_url"],
                "created_at": r["created_at"],
            })
        return {"subscriptions": result}
    finally:
        conn.close()


@app.post("/api/subscriptions")
async def api_subscription_create(request: Request, user=Depends(require_login)):
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="订阅名称不能为空")
    frequency = body.get("frequency", "daily")
    if frequency not in ("daily", "weekly", "instant"):
        frequency = "daily"
    conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO subscriptions (user_id, name, email, keywords, regions, categories, buckets, frequency, webhook_url)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                user["id"],
                name,
                (body.get("notify_email") or "").strip(),
                json.dumps(body.get("keywords") or [], ensure_ascii=False),
                json.dumps(body.get("regions") or [], ensure_ascii=False),
                json.dumps(body.get("categories") or [], ensure_ascii=False),
                json.dumps(body.get("buckets") or [], ensure_ascii=False),
                frequency,
                (body.get("webhook_url") or "").strip(),
            ),
        )
        conn.commit()
        return {"id": cur.lastrowid, "ok": True}
    finally:
        conn.close()


@app.delete("/api/subscriptions/{sub_id}")
async def api_subscription_delete(sub_id: int, user=Depends(require_login)):
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE subscriptions SET active=0 WHERE id=? AND user_id=?",
            (sub_id, user["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


# ── Digest & Report API（stub）───────────────────────────────────────────────

@app.get("/api/digest/daily")
async def api_digest_daily(days: int = 1, brief: bool = False, user=Depends(require_login)):
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, title, url, date, source_name, region, categories FROM items WHERE date >= date('now', ? || ' days') ORDER BY date DESC LIMIT 20",
            (f"-{days}",),
        ).fetchall()
        items = []
        for r in rows:
            cats = _parse_json_field(r["categories"])
            items.append({
                "id": r["id"],
                "title": r["title"],
                "url": r["url"] or "",
                "date": r["date"],
                "source_name": r["source_name"] or "",
                "region": r["region"] or "全国",
                "bucket": _classify_bucket(cats),
            })
        return {"days": days, "items": items, "total": len(items)}
    finally:
        conn.close()


@app.get("/api/report/weekly")
async def api_report_weekly(format: str = "md", user=Depends(require_login)):
    stats = _get_stats()
    report = f"# 储能政策情报周报\n\n生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
    report += f"## 数据概览\n- 总收录：{stats['total']} 条\n- 近7天新增：{stats['recent_7d']} 条\n"
    return {"format": format, "content": report}


# ── Scope API（stub）─────────────────────────────────────────────────────────

@app.get("/api/scope")
async def api_scope(user=Depends(require_login)):
    return {"config": {}}


@app.post("/api/scope")
async def api_scope_save(request: Request, user=Depends(require_login)):
    return {"ok": True}


# ── Scrape Status API（stub）─────────────────────────────────────────────────

@app.get("/api/scrape-status")
async def api_scrape_status(user=Depends(require_login)):
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT source_name, MAX(created_at) as last_scraped, COUNT(*) as cnt FROM items GROUP BY source_name ORDER BY cnt DESC LIMIT 20"
        ).fetchall()
        sources = []
        for r in rows:
            sources.append({
                "name": r["source_name"],
                "last_scraped": r["last_scraped"],
                "count": r["cnt"],
                "status": "ok",
            })
        return {"sources": sources, "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M")}
    finally:
        conn.close()


# ── Profile API ───────────────────────────────────────────────────────────────

@app.get("/api/profile")
async def api_get_profile(user=Depends(require_login)):
    profile = _get_user_profile(user["id"])
    return profile


@app.post("/api/profile")
async def api_save_profile(request: Request, user=Depends(require_login)):
    body = await request.json()
    company_type   = (body.get("company_type") or "").strip()
    provinces      = body.get("provinces", [])
    business_stage = (body.get("business_stage") or "全阶段").strip()

    if not isinstance(provinces, list):
        provinces = []
    provinces_json = json.dumps(provinces, ensure_ascii=False)

    conn = get_conn()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO user_profiles
               (user_id, company_type, provinces, business_stage, updated_at)
               VALUES (?, ?, ?, ?, datetime('now','localtime'))""",
            (user["id"], company_type, provinces_json, business_stage),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


# ── Impact Score API ──────────────────────────────────────────────────────────

@app.get("/api/items/{item_id}/impact")
async def api_item_impact(item_id: int, user=Depends(require_paid)):
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "政策不存在")
    policy = _item_to_dict(row)
    user_profile = _get_user_profile(user["id"])
    return calculate_impact_score(user_profile, policy)


# ── AI API（付费）────────────────────────────────────────────────────────────

def _get_openai_client():
    try:
        from openai import OpenAI
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise HTTPException(503, "AI 服务未配置（OPENAI_API_KEY 未设置）")
        return OpenAI(api_key=api_key)
    except ImportError:
        raise HTTPException(503, "AI 依赖未安装（openai）")


@app.post("/api/ai/search")
async def ai_search(request: Request, user=Depends(require_paid)):
    body = await request.json()
    query = (body.get("query") or "").strip()
    if not query:
        raise HTTPException(400, "查询内容不能为空")

    conn = get_conn()
    try:
        words = query.split()
        conditions = []
        params = []
        for w in words[:5]:
            like = f"%{w}%"
            conditions.append("(title LIKE ? OR summary LIKE ?)")
            params += [like, like]
        where = "WHERE " + " OR ".join(conditions) if conditions else ""
        rows = conn.execute(
            f"SELECT id, title, summary, region, date, level, status FROM items {where} ORDER BY date DESC LIMIT 60",
            params,
        ).fetchall()
        if len(rows) < 10:
            rows = conn.execute(
                "SELECT id, title, summary, region, date, level, status FROM items ORDER BY date DESC LIMIT 50"
            ).fetchall()
    finally:
        conn.close()

    candidate_items = [dict(r) for r in rows]
    items_text = "\n".join([
        f"{i+1}. [{r.get('region','未知')}] {r['title']} ({r.get('date','')}) [{r.get('level','')}]"
        + (f"\n   {(r.get('summary') or '')[:150]}" if r.get("summary") else "")
        for i, r in enumerate(candidate_items[:40])
    ])

    client = _get_openai_client()

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=2000,
        messages=[
            {"role": "system", "content": "你是中国储能电力政策专家助手。请严格按 JSON 格式回复，不要添加任何其他内容。"},
            {"role": "user", "content": f"""用户查询："{query}"

以下是数据库中的政策条目：
{items_text}

请从上述列表中找出与用户查询最相关的条目，返回以下 JSON：
{{
  "summary": "针对用户查询的整体背景说明（2-3句话）",
  "results": [
    {{"number": 1, "relevance": "高/中/低", "key_point": "与查询的关联要点（1句话）"}}
  ]
}}
最多返回 10 条，按相关度从高到低排列。只返回 JSON。"""},
        ],
    )

    ai_text = response.choices[0].message.content.strip()
    if "```" in ai_text:
        import re
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", ai_text, re.DOTALL)
        if m:
            ai_text = m.group(1)

    try:
        ai_result = json.loads(ai_text)
    except Exception:
        ai_result = {"summary": "AI 分析完成", "results": []}

    final_items = []
    for r in ai_result.get("results", []):
        idx = int(r.get("number", 0)) - 1
        if 0 <= idx < len(candidate_items):
            item = candidate_items[idx].copy()
            item["ai_relevance"] = r.get("relevance", "")
            item["ai_key_point"] = r.get("key_point", "")
            final_items.append(item)

    return {"query": query, "summary": ai_result.get("summary", ""), "items": final_items, "total": len(final_items)}


@app.get("/api/ai/interpret/{item_id}")
async def ai_interpret(item_id: int, user=Depends(require_paid)):
    conn = get_conn()
    try:
        cached = conn.execute(
            "SELECT analysis_data FROM item_analyses WHERE item_id=?", (item_id,)
        ).fetchone()
        if cached:
            return json.loads(cached["analysis_data"])
        row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    finally:
        conn.close()

    if not row:
        raise HTTPException(404, "政策不存在")

    item = _item_to_dict(row)
    content_text = f"""政策标题：{item['title']}
地区：{item.get('region', '未知')}
时间：{item.get('date', '未知')}
层级：{item.get('level', '未知')}
状态：{item.get('status', '未知')}
分类：{', '.join(item.get('categories', []))}
摘要：{item.get('summary') or '（无摘要）'}
{('正文：' + (item.get('content') or '')[:3000]) if item.get('content') else ''}"""

    client = _get_openai_client()
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=2000,
        messages=[
            {"role": "system", "content": "你是中国储能电力政策专家。请用简洁、专业的中文解读政策，严格按 JSON 格式回复。"},
            {"role": "user", "content": f"""请解读以下政策文件：

{content_text}

请返回 JSON：
{{
  "core_content": "政策核心内容（3-5个要点，每点一句话，用分号分隔）",
  "key_requirements": ["关键要求1", "关键要求2", "关键要求3"],
  "impact_analysis": {{
    "developers": "对电站开发商/投资方的影响",
    "manufacturers": "对设备制造商的影响",
    "grid": "对电网公司/调度机构的影响"
  }},
  "action_items": ["建议行动1", "建议行动2", "建议行动3"],
  "timeline": "重要时间节点（如有，否则填无）",
  "overall_assessment": "总体评价及政策走向判断（1-2句话）"
}}
只返回 JSON。"""},
        ],
    )

    ai_text = response.choices[0].message.content.strip()
    if "```" in ai_text:
        import re
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", ai_text, re.DOTALL)
        if m:
            ai_text = m.group(1)

    try:
        result = json.loads(ai_text)
    except Exception:
        result = {
            "core_content": "AI 解读暂时不可用",
            "key_requirements": [],
            "impact_analysis": {"developers": "", "manufacturers": "", "grid": ""},
            "action_items": [],
            "timeline": "",
            "overall_assessment": "",
        }

    conn = get_conn()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO item_analyses (item_id, analysis_data, updated_at)
               VALUES (?, ?, datetime('now','localtime'))""",
            (item_id, json.dumps(result, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()

    return result


# ── 管理接口 ──────────────────────────────────────────────────────────────────

_ADMIN_KEY = os.getenv("ADMIN_KEY", "")


@app.post("/admin/grant-paid")
async def admin_grant_paid(request: Request):
    if not _ADMIN_KEY:
        raise HTTPException(403, "ADMIN_KEY 未配置")
    if request.headers.get("X-Admin-Key") != _ADMIN_KEY:
        raise HTTPException(403, "无效的管理员密钥")

    body = await request.json()
    username = (body.get("username") or "").strip()
    is_paid = int(bool(body.get("is_paid", True)))

    conn = get_conn()
    try:
        r = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if not r:
            raise HTTPException(404, "用户不存在")
        conn.execute("UPDATE users SET is_paid=? WHERE username=?", (is_paid, username))
        conn.commit()
    finally:
        conn.close()

    return {"ok": True, "username": username, "is_paid": bool(is_paid)}


# ── Legacy HTML routes（保持兼容）────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page():
    html_path = BASE_DIR / "templates" / "app.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.get("/register", response_class=HTMLResponse)
async def register_page():
    html_path = BASE_DIR / "templates" / "app.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie("access_token")
    return resp


@app.get("/compare", response_class=HTMLResponse)
async def compare_page():
    html_path = BASE_DIR / "templates" / "app.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.get("/profile", response_class=HTMLResponse)
async def profile_page():
    html_path = BASE_DIR / "templates" / "app.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
