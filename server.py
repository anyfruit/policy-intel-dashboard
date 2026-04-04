"""server.py — 储能电力政策库 Web 服务

免费版（free）：浏览最新 Top 10、基础搜索、单条详情
基础版（basic, ¥99/月）：全量浏览 + 筛选、邮件订阅推送、详情全文
专业版（pro, ¥299/月）：所有基础版功能 + AI 检索/解读、跨省对比、影响评分、周报导出
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
    """返回完整用户信息（含 plan / is_paid）或 None。"""
    payload = auth._verify_token(access_token or "")
    if not payload:
        return None
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, username, email, is_paid, plan FROM users WHERE id=?",
            (int(payload["uid"]),),
        ).fetchone()
        if not row:
            return None
        u = dict(row)
        # 向后兼容：plan 字段未设置时，用 is_paid 推导
        if not u.get("plan") or u["plan"] == "free":
            if u.get("is_paid"):
                u["plan"] = "pro"
        # 反向同步：is_paid 保持与 plan 一致
        u["is_paid"] = 1 if u.get("plan") in ("basic", "pro") else 0
        return u
    finally:
        conn.close()


def require_login(user=Depends(_get_user_with_plan)) -> dict:
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


def require_basic(user=Depends(_get_user_with_plan)) -> dict:
    """基础版及以上（basic / pro）。"""
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    if user.get("plan") not in ("basic", "pro"):
        raise HTTPException(status_code=402, detail="此功能需要基础版或专业版订阅（¥99/月起），请联系管理员升级。")
    return user


def require_pro(user=Depends(_get_user_with_plan)) -> dict:
    """专业版（pro）专属。"""
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    if user.get("plan") != "pro":
        raise HTTPException(status_code=402, detail="此功能需要专业版订阅（¥299/月），请联系管理员升级。")
    return user


def require_paid(user=Depends(_get_user_with_plan)) -> dict:
    """向后兼容别名 → 等同 require_pro。"""
    return require_pro(user)


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def _parse_json_field(v) -> list:
    if isinstance(v, list):
        return v
    try:
        return json.loads(v or "[]")
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
    plan = (user or {}).get("plan", "free")
    # 免费版：只展示最新 10 条，不允许筛选翻页
    if plan == "free":
        items, total = _query_items("", "", "", "", "", 1, limit=10)
        page, total_pages = 1, 1
    else:
        items, total = _query_items(q, region, level, status, category, page, limit=20)
        total_pages = max(1, (total + 19) // 20)
    regions = _get_regions()
    stats = _get_stats()

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
async def ai_search(request: Request, user=Depends(require_pro)):
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
async def ai_interpret(item_id: int, user=Depends(require_pro)):
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


# ── 跨省对比（Step 5）─────────────────────────────────────────────────────────

@app.get("/compare", response_class=HTMLResponse)
async def compare_page(request: Request, user=Depends(_get_user_with_plan)):
    regions = _get_regions()
    return templates.TemplateResponse("compare.html", {
        "request": request,
        "user": user,
        "regions": regions,
        "category_types": CATEGORY_TYPES,
    })


@app.get("/api/compare")
async def api_compare(
    provinces: str = "",
    category: str = "",
    user=Depends(require_basic),
):
    """跨省政策对比（基础版及以上）。
    provinces: 逗号分隔的省份列表，如 "江苏,浙江,安徽"
    category:  可选，政策分类过滤
    """
    province_list = [p.strip() for p in provinces.split(",") if p.strip()]
    if not province_list:
        raise HTTPException(400, "请至少指定一个省份")
    if len(province_list) > 6:
        raise HTTPException(400, "最多对比 6 个省份")

    conn = get_conn()
    try:
        result = []
        for prov in province_list:
            cat_clause = "AND categories LIKE ?" if category else ""
            cat_params = [f"%{category}%"] if category else []

            count = conn.execute(
                f"SELECT COUNT(*) FROM items WHERE region=? {cat_clause} AND (canonical_id IS NULL)",
                [prov] + cat_params,
            ).fetchone()[0]

            latest_row = conn.execute(
                f"SELECT id, title, date, level, status FROM items WHERE region=? {cat_clause} AND (canonical_id IS NULL) ORDER BY date DESC LIMIT 1",
                [prov] + cat_params,
            ).fetchone()

            # 最近 90 天新政策数
            recent = conn.execute(
                f"SELECT COUNT(*) FROM items WHERE region=? {cat_clause} AND (canonical_id IS NULL) AND date >= date('now','-90 days')",
                [prov] + cat_params,
            ).fetchone()[0]

            # 各状态分布
            status_rows = conn.execute(
                f"SELECT status, COUNT(*) as cnt FROM items WHERE region=? {cat_clause} AND (canonical_id IS NULL) GROUP BY status",
                [prov] + cat_params,
            ).fetchall()
            status_dist = {r["status"]: r["cnt"] for r in status_rows}

            result.append({
                "province": prov,
                "count": count,
                "recent_90d": recent,
                "latest": dict(latest_row) if latest_row else None,
                "status_distribution": status_dist,
            })
    finally:
        conn.close()

    return {
        "provinces": province_list,
        "category": category,
        "data": result,
    }


@app.post("/api/ai/compare")
async def api_ai_compare(request: Request, user=Depends(require_pro)):
    """AI 深度跨省对比分析（专业版）。"""
    body = await request.json()
    provinces = body.get("provinces") or []
    category = (body.get("category") or "").strip()

    if not provinces or len(provinces) < 2:
        raise HTTPException(400, "请至少指定两个省份")

    # 先获取各省政策摘要
    conn = get_conn()
    try:
        province_summaries = {}
        for prov in provinces[:6]:
            cat_clause = "AND categories LIKE ?" if category else ""
            cat_params = [f"%{category}%"] if category else []
            rows = conn.execute(
                f"SELECT title, summary, date, level, status FROM items WHERE region=? {cat_clause} AND (canonical_id IS NULL) ORDER BY date DESC LIMIT 10",
                [prov] + cat_params,
            ).fetchall()
            province_summaries[prov] = [dict(r) for r in rows]
    finally:
        conn.close()

    # 构建给 Claude 的上下文
    context_parts = []
    for prov, policies in province_summaries.items():
        if not policies:
            context_parts.append(f"## {prov}\n（无相关政策）\n")
            continue
        lines = [f"## {prov}（共检索到 {len(policies)} 条）"]
        for p in policies:
            lines.append(f"- {p['date'] or ''}  [{p['level'] or ''}] {p['title']}")
            if p.get("summary"):
                lines.append(f"  摘要：{p['summary'][:100]}")
        context_parts.append("\n".join(lines))

    context = "\n\n".join(context_parts)
    cat_note = f"（分类：{category}）" if category else ""

    client = _get_anthropic_client()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2500,
        system="你是中国储能电力政策专家。请用简洁、专业的中文回复，严格按 JSON 格式。",
        messages=[{
            "role": "user",
            "content": f"""请对以下各省的储能电力政策{cat_note}进行横向对比分析：

{context}

请返回 JSON：
{{
  "summary": "整体对比概述（2-3句话）",
  "province_profiles": [
    {{
      "province": "省份名",
      "strengths": "政策优势（1-2句）",
      "gaps": "政策短板或空白（1句）",
      "activity_level": "高/中/低"
    }}
  ],
  "key_differences": ["关键差异点1", "关键差异点2", "关键差异点3"],
  "recommended_province": "综合推荐省份",
  "recommendation_reason": "推荐理由（1-2句）"
}}
只返回 JSON。""",
        }],
    )

    ai_text = response.content[0].text.strip()
    if "```" in ai_text:
        import re as _re
        m = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", ai_text, _re.DOTALL)
        if m:
            ai_text = m.group(1)

    try:
        ai_result = json.loads(ai_text)
    except Exception:
        ai_result = {
            "summary": "AI 分析暂时不可用",
            "province_profiles": [],
            "key_differences": [],
            "recommended_province": "",
            "recommendation_reason": "",
        }

    return {"provinces": provinces, "category": category, "ai_analysis": ai_result}


# ── 订阅管理（Step 6b）────────────────────────────────────────────────────────

@app.get("/subscribe", response_class=HTMLResponse)
async def subscribe_page(request: Request, user=Depends(require_basic)):
    conn = get_conn()
    try:
        sub = conn.execute(
            "SELECT * FROM subscriptions WHERE user_id=?", (user["id"],)
        ).fetchone()
    finally:
        conn.close()
    regions = _get_regions()
    import notifier
    return templates.TemplateResponse("subscribe.html", {
        "request": request,
        "user": user,
        "sub": dict(sub) if sub else None,
        "regions": regions,
        "category_types": CATEGORY_TYPES,
        "smtp_ok": notifier.smtp_configured(),
    })


@app.post("/subscribe")
async def subscribe_submit(
    request: Request,
    frequency: str = Form("weekly"),
    filter_region: str = Form(""),
    filter_category: str = Form(""),
    user=Depends(require_basic),
):
    if frequency not in ("daily", "weekly"):
        frequency = "weekly"
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO subscriptions (user_id, frequency, filter_region, filter_category)
               VALUES (?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 frequency=excluded.frequency,
                 filter_region=excluded.filter_region,
                 filter_category=excluded.filter_category""",
            (user["id"], frequency, filter_region, filter_category),
        )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/subscribe?saved=1", status_code=303)


@app.delete("/subscribe")
async def subscribe_delete(user=Depends(require_basic)):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM subscriptions WHERE user_id=?", (user["id"],))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


# ── 管理接口 ──────────────────────────────────────────────────────────────────

_ADMIN_KEY = os.getenv("ADMIN_KEY", "")


@app.post("/admin/grant-paid")
async def admin_grant_paid(request: Request):
    """管理员给用户设置订阅套餐。需在请求 header 带 X-Admin-Key。
    Body: { "username": "...", "plan": "free|basic|pro" }
    也兼容旧接口: { "username": "...", "is_paid": true/false }
    """
    if not _ADMIN_KEY:
        raise HTTPException(403, "ADMIN_KEY 未配置")
    if request.headers.get("X-Admin-Key") != _ADMIN_KEY:
        raise HTTPException(403, "无效的管理员密钥")

    body = await request.json()
    username = (body.get("username") or "").strip()

    # 新接口：plan 字段
    plan = (body.get("plan") or "").strip()
    if plan not in ("free", "basic", "pro"):
        # 向后兼容旧接口
        is_paid = bool(body.get("is_paid", True))
        plan = "pro" if is_paid else "free"

    is_paid_int = 1 if plan in ("basic", "pro") else 0

    conn = get_conn()
    try:
        r = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if not r:
            raise HTTPException(404, "用户不存在")
        conn.execute(
            "UPDATE users SET is_paid=?, plan=? WHERE username=?",
            (is_paid_int, plan, username),
        )
        conn.commit()
    finally:
        conn.close()

    return {"ok": True, "username": username, "plan": plan, "is_paid": bool(is_paid_int)}
