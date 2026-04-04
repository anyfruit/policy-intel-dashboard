"""server.py — 储能电力政策库 Web 服务

免费版：浏览政策列表、按条件筛选、查看政策详情
付费版：AI 智能检索（语义搜索）、AI 政策解读
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from fastapi import Cookie, Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import auth
import db
from db import get_conn
from tags import CATEGORY_TYPES

app = FastAPI(title="储能电力政策库", docs_url=None, redoc_url=None)

BASE_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# ── 依赖 ─────────────────────────────────────────────────────────────────────

def _get_user_with_plan(access_token: Optional[str] = Cookie(default=None)) -> Optional[dict]:
    """返回完整用户信息（含 is_paid）或 None。"""
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
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


def require_paid(user=Depends(_get_user_with_plan)) -> dict:
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    if not user.get("is_paid"):
        raise HTTPException(status_code=402, detail="此功能需要付费订阅，请联系管理员升级账户。")
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
    page: int = 1,
    limit: int = 20,
) -> tuple[list[dict], int]:
    """全文检索 + 多维筛选，返回 (items, total)。"""
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
        recent = conn.execute(
            "SELECT COUNT(*) FROM items WHERE date >= date('now','-30 days')"
        ).fetchone()[0]
        return {
            "total": total,
            "recent_30d": recent,
            "by_level": {r["level"]: r["cnt"] for r in by_level},
            "by_status": {r["status"]: r["cnt"] for r in by_status},
        }
    finally:
        conn.close()


# ── 页面路由 ──────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    db.init_db()
    auth.init_users_table()


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    q: str = "",
    region: str = "",
    level: str = "",
    status: str = "",
    category: str = "",
    page: int = 1,
    user=Depends(_get_user_with_plan),
):
    items, total = _query_items(q, region, level, status, category, page, limit=20)
    regions = _get_regions()
    stats = _get_stats()
    total_pages = max(1, (total + 19) // 20)

    return templates.TemplateResponse("index.html", {
        "request": request,
        "user": user,
        "items": items,
        "total": total,
        "page": page,
        "total_pages": total_pages,
        "q": q,
        "region": region,
        "level": level,
        "status": status,
        "category": category,
        "regions": regions,
        "stats": stats,
        "category_types": CATEGORY_TYPES,
        "levels": ["国家", "省", "市", "行业"],
        "statuses": ["现行", "征求意见", "废止"],
    })


@app.get("/items/{item_id}", response_class=HTMLResponse)
async def item_detail(
    request: Request,
    item_id: int,
    user=Depends(_get_user_with_plan),
):
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    finally:
        conn.close()

    if not row:
        raise HTTPException(404, "政策不存在")

    item = _item_to_dict(row)

    # 相关政策（同地区或同分类，最多5条）
    region = item.get("region") or ""
    conn = get_conn()
    try:
        related = conn.execute(
            "SELECT id, title, date, level FROM items WHERE region=? AND id!=? ORDER BY date DESC LIMIT 5",
            (region, item_id),
        ).fetchall()
    finally:
        conn.close()

    return templates.TemplateResponse("item.html", {
        "request": request,
        "user": user,
        "item": item,
        "related": [dict(r) for r in related],
    })


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, user=Depends(_get_user_with_plan)):
    if user:
        return RedirectResponse("/")
    return templates.TemplateResponse("login.html", {"request": request, "user": None, "error": ""})


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    u = auth.authenticate_user(username, password)
    if not u:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "user": None, "error": "用户名或密码错误"},
            status_code=400,
        )
    token = auth.create_access_token(u["id"], u["username"])
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("access_token", token, httponly=True, max_age=auth._TOKEN_TTL_SEC, samesite="lax")
    return resp


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, user=Depends(_get_user_with_plan)):
    if user:
        return RedirectResponse("/")
    return templates.TemplateResponse("register.html", {"request": request, "user": None, "error": ""})


@app.post("/register")
async def register_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
):
    if password != password2:
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "user": None, "error": "两次密码不一致"},
            status_code=400,
        )
    try:
        u = auth.create_user(username, password)
    except ValueError as e:
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "user": None, "error": str(e)},
            status_code=400,
        )
    token = auth.create_access_token(u["id"], u["username"])
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("access_token", token, httponly=True, max_age=auth._TOKEN_TTL_SEC, samesite="lax")
    return resp


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie("access_token")
    return resp


# ── JSON API ──────────────────────────────────────────────────────────────────

@app.get("/api/items")
async def api_items(
    q: str = "",
    region: str = "",
    level: str = "",
    status: str = "",
    category: str = "",
    page: int = 1,
    limit: int = 20,
):
    limit = min(limit, 100)
    items, total = _query_items(q, region, level, status, category, page, limit)
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
    return _item_to_dict(row)


@app.get("/api/stats")
async def api_stats():
    return _get_stats()


# ── AI 功能（付费）────────────────────────────────────────────────────────────

def _get_anthropic_client():
    try:
        import anthropic
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise HTTPException(503, "AI 服务未配置（ANTHROPIC_API_KEY 未设置）")
        return anthropic.Anthropic(api_key=api_key)
    except ImportError:
        raise HTTPException(503, "AI 依赖未安装（anthropic）")


@app.post("/api/ai/search")
async def ai_search(request: Request, user=Depends(require_paid)):
    body = await request.json()
    query = (body.get("query") or "").strip()
    if not query:
        raise HTTPException(400, "查询内容不能为空")

    # 先用关键词在数据库做初步检索（最多 60 条）
    conn = get_conn()
    try:
        # 尝试多个词分别搜索
        words = query.split()
        conditions = []
        params = []
        for w in words[:5]:
            like = f"%{w}%"
            conditions.append("(title LIKE ? OR summary LIKE ?)")
            params += [like, like]

        if conditions:
            where = "WHERE " + " OR ".join(conditions)
        else:
            where = ""

        rows = conn.execute(
            f"SELECT id, title, summary, region, date, level, status FROM items {where} ORDER BY date DESC LIMIT 60",
            params,
        ).fetchall()

        # 如果关键词匹配太少，补充最近的条目
        if len(rows) < 10:
            rows = conn.execute(
                "SELECT id, title, summary, region, date, level, status FROM items ORDER BY date DESC LIMIT 50"
            ).fetchall()
    finally:
        conn.close()

    candidate_items = [dict(r) for r in rows]

    # 构建给 Claude 的上下文
    items_text = "\n".join([
        f"{i+1}. [{r.get('region','未知')}] {r['title']} ({r.get('date','')}) [{r.get('level','')}]"
        + (f"\n   {(r.get('summary') or '')[:150]}" if r.get("summary") else "")
        for i, r in enumerate(candidate_items[:40])
    ])

    client = _get_anthropic_client()
    import anthropic

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        system="你是中国储能电力政策专家助手。请严格按 JSON 格式回复，不要添加任何其他内容。",
        messages=[{
            "role": "user",
            "content": f"""用户查询："{query}"

以下是数据库中的政策条目（编号 · 地区 · 标题 · 日期 · 层级 · 摘要节选）：
{items_text}

请从上述列表中找出与用户查询最相关的条目，按相关度排序，返回以下 JSON：
{{
  "summary": "针对用户查询的整体背景说明（2-3句话）",
  "results": [
    {{"number": 1, "relevance": "高/中/低", "key_point": "与查询的关联要点（1句话）"}},
    ...
  ]
}}
最多返回 10 条最相关的，按相关度从高到低排列。只返回 JSON。""",
        }],
    )

    ai_text = response.content[0].text.strip()
    # 容错：提取 JSON 块
    if "```" in ai_text:
        import re
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", ai_text, re.DOTALL)
        if m:
            ai_text = m.group(1)

    try:
        ai_result = json.loads(ai_text)
    except Exception:
        ai_result = {
            "summary": "AI 分析完成",
            "results": [{"number": i + 1, "relevance": "中", "key_point": ""} for i in range(min(5, len(candidate_items)))],
        }

    # 组装最终结果
    final_items = []
    for r in ai_result.get("results", []):
        idx = int(r.get("number", 0)) - 1
        if 0 <= idx < len(candidate_items):
            item = candidate_items[idx].copy()
            item["ai_relevance"] = r.get("relevance", "")
            item["ai_key_point"] = r.get("key_point", "")
            final_items.append(item)

    return {
        "query": query,
        "summary": ai_result.get("summary", ""),
        "items": final_items,
        "total": len(final_items),
    }


@app.get("/api/ai/interpret/{item_id}")
async def ai_interpret(item_id: int, user=Depends(require_paid)):
    # 检查缓存
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

    client = _get_anthropic_client()

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        system="你是中国储能电力政策专家。请用简洁、专业的中文解读政策，严格按 JSON 格式回复。",
        messages=[{
            "role": "user",
            "content": f"""请解读以下政策文件：

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
只返回 JSON。""",
        }],
    )

    ai_text = response.content[0].text.strip()
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

    # 写入缓存
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
    """管理员给用户开通付费权限。需在请求 header 带 X-Admin-Key。"""
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
