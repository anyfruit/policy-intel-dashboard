#!/usr/bin/env python3
"""
scrape_provinces_via_search.py
用 Brave Search API 搜索各省储能政策，通过行业媒体/镜像访问
（解决直接访问省级政府网站被封锁的问题）

用法:
    export BRAVE_API_KEY=your_key
    python scrape_provinces_via_search.py
    python scrape_provinces_via_search.py --dry-run
    python scrape_provinces_via_search.py --province 江苏
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, date
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent))
from db import init_db, upsert_item_with_version, DB_PATH
from tags import extract_tags, infer_categories, extract_impact_on, infer_status, infer_level

BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "")
BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"

# 直辖市（source_name 用"市"而非"省"）
MUNICIPALITIES = {"北京", "上海", "天津", "重庆"}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 Chrome/120 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# 来源白名单：可访问的行业媒体/政策聚合站
ACCESSIBLE_DOMAINS = [
    "chuneng.bjx.com.cn",   # 北极星储能
    "bjx.com.cn",
    "eschinaren.com",        # 储能100人
    "in-en.com",             # 国际能源网
    "sohu.com",
    "163.com",
    "thepaper.cn",           # 澎湃新闻
    "gov.cn",                # 国务院/部委（能访问的）
    "nea.gov.cn",
    "ndrc.gov.cn",
    "cnesa.org",
    "cec.org.cn",
    "cpca.org.cn",
]

# 各省搜索配置
PROVINCE_QUERIES = [
    # (province_name, queries, source_id)
    ("江苏", [
        "江苏省 储能政策 发改委 2024 OR 2025 OR 2026",
        "江苏省 新型储能 管理办法 site:chuneng.bjx.com.cn OR site:in-en.com",
    ], "jiangsu_search"),
    ("湖南", [
        "湖南省 储能政策 发改委 2024 OR 2025 OR 2026",
        "湖南 新型储能 实施方案 site:chuneng.bjx.com.cn OR site:in-en.com",
    ], "hunan_search"),
    ("湖北", [
        "湖北省 储能政策 发改委 2024 OR 2025 OR 2026",
        "湖北 储能电站 管理规定 site:chuneng.bjx.com.cn",
    ], "hubei_search"),
    ("北京", [
        "北京市 储能政策 发改委 2024 OR 2025 OR 2026",
        "北京 新型储能 site:chuneng.bjx.com.cn OR site:bjx.com.cn",
    ], "beijing_search"),
    ("四川", [
        "四川省 储能政策 发改委 2024 OR 2025 OR 2026",
        "四川 储能电站 site:chuneng.bjx.com.cn OR site:in-en.com",
    ], "sichuan_search"),
    ("安徽", [
        "安徽省 储能政策 发改委 2024 OR 2025 OR 2026",
        "安徽 新型储能 site:chuneng.bjx.com.cn",
    ], "anhui_search"),
    ("福建", [
        "福建省 储能政策 发改委 2024 OR 2025 OR 2026",
        "福建 储能 补贴 site:chuneng.bjx.com.cn",
    ], "fujian_search"),
    ("内蒙古", [
        "内蒙古 储能政策 发改委 2024 OR 2025 OR 2026",
        "内蒙古 新能源储能 site:chuneng.bjx.com.cn OR site:bjx.com.cn",
    ], "neimenggu_search"),
    ("宁夏", [
        "宁夏 储能政策 发改委 2024 OR 2025 OR 2026",
        "宁夏 新型储能 site:chuneng.bjx.com.cn",
    ], "ningxia_search"),
    ("新疆", [
        "新疆 储能政策 2024 OR 2025 OR 2026",
        "新疆 储能电站 配置 site:chuneng.bjx.com.cn OR site:bjx.com.cn",
    ], "xinjiang_search"),
    ("河南", [
        "河南省 储能政策 发改委 2024 OR 2025 OR 2026",
        "河南 独立储能 site:chuneng.bjx.com.cn",
    ], "henan_search"),
    ("湖南", [
        "湖南省 储能政策 2024 OR 2025 OR 2026",
    ], "hunan_search"),
]


def brave_search(query: str, count: int = 10) -> list[dict]:
    if not BRAVE_API_KEY:
        print("⚠️  未设置 BRAVE_API_KEY，跳过搜索")
        return []
    try:
        r = requests.get(
            BRAVE_SEARCH_URL,
            headers={"Accept": "application/json", "X-Subscription-Token": BRAVE_API_KEY},
            params={"q": query, "count": count, "search_lang": "zh-hans", "country": "CN"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        return data.get("web", {}).get("results", [])
    except Exception as e:
        print(f"  搜索失败: {e}")
        return []


def fetch_article(url: str) -> tuple[str, str]:
    """抓取文章页面，返回 (title, content)"""
    try:
        r = requests.get(url, headers=HEADERS, timeout=8, verify=False)
        r.encoding = r.apparent_encoding or "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")
        # 提取标题
        title = ""
        for sel in ["h1", "h2", ".article-title", ".title", "title"]:
            el = soup.select_one(sel)
            if el:
                title = el.get_text(strip=True)
                break
        # 提取正文
        content = ""
        for sel in [".article-content", ".content", ".artcle", "article", ".text", ".detail"]:
            el = soup.select_one(sel)
            if el:
                content = el.get_text(" ", strip=True)[:3000]
                break
        if not content:
            body = soup.find("body")
            if body:
                content = body.get_text(" ", strip=True)[:2000]
        return title, content
    except Exception as e:
        return "", ""


def extract_date_from_text(text: str) -> Optional[str]:
    """从文本中提取发布日期"""
    patterns = [
        r"(\d{4})[-年](\d{1,2})[-月](\d{1,2})[日号]?",
        r"(\d{4})/(\d{1,2})/(\d{1,2})",
    ]
    for p in patterns:
        m = re.search(p, text[:500])
        if m:
            try:
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if 2020 <= y <= 2030 and 1 <= mo <= 12 and 1 <= d <= 31:
                    return f"{y:04d}-{mo:02d}-{d:02d}"
            except Exception:
                pass
    return None


def is_relevant_url(url: str) -> bool:
    """过滤明显不相关的 URL"""
    exclude = ["login", "register", "search?", "tag/", "/author/", "comment", "about"]
    return not any(e in url.lower() for e in exclude)


def _domain_allowed(url: str) -> bool:
    """检查 URL 域名是否在白名单内（支持子域名匹配）"""
    try:
        host = urlparse(url).netloc.lower().split(":")[0]
        return any(host == d or host.endswith("." + d) for d in ACCESSIBLE_DOMAINS)
    except Exception:
        return False


def process_province(province: str, queries: list[str], source_id: str,
                     dry_run: bool = False) -> int:
    print(f"\n🗺️  {province}（source_id={source_id}）")
    seen_urls = set()
    imported = 0
    domain_filtered = 0

    # 直辖市用"市"，其他用"省"
    province_type = "市" if province in MUNICIPALITIES else "省"

    for query in queries:
        print(f"  🔍 搜索: {query[:60]}")
        results = brave_search(query, count=10)
        time.sleep(0.5)

        for r in results:
            url = r.get("url", "")
            title = r.get("title", "").strip()
            snippet = r.get("description", "").strip()

            if not url or not title or url in seen_urls:
                continue
            if not is_relevant_url(url):
                continue

            # 域名白名单硬过滤
            if not _domain_allowed(url):
                domain_filtered += 1
                continue

            # 储能相关性快速过滤
            combined = (title + " " + snippet).lower()
            keywords = ["储能", "电化学", "bess", "锂电", "液流", "pcs", "bms"]
            if not any(k in combined for k in keywords):
                continue

            seen_urls.add(url)

            # 提取日期
            pub_date = r.get("page_age") or extract_date_from_text(title + " " + snippet)

            # 抓取全文正文
            content = ""
            try:
                _, fetched_content = fetch_article(url)
                if fetched_content:
                    content = fetched_content
            except Exception:
                pass

            # 用于分类/标签的文本（标题+摘要+正文前500字）
            analyze_text = title + " " + snippet + " " + content[:500]

            # 构建 item
            item = {
                "url": url,
                "title": title[:500],
                "summary": (snippet[:400] if snippet else ""),
                "content": content,
                "date": pub_date,
                "source_id": source_id,
                "source_name": f"{province}{province_type}政策（搜索来源）",
                "region": province,
                "categories": infer_categories(analyze_text),
                "tags": extract_tags(analyze_text),
                "impact_on": extract_impact_on(analyze_text),
                "level": infer_level(source_id, title),
                "status": infer_status(title),
            }

            print(f"    ✓ {title[:50]}")

            if not dry_run:
                try:
                    upsert_item_with_version(item)
                    imported += 1
                except Exception as e:
                    print(f"      ⚠️ 入库失败: {e}")
            else:
                imported += 1

    if domain_filtered:
        print(f"  🚫 域名过滤: {domain_filtered} 条（不在白名单）")
    print(f"  📥 {province}: {'(dry-run) ' if dry_run else ''}导入 {imported} 条")
    return imported


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--province", help="只处理指定省份")
    args = p.parse_args()

    if not BRAVE_API_KEY:
        print("❌ 请先设置 BRAVE_API_KEY 环境变量")
        sys.exit(1)

    init_db()
    total = 0
    for province, queries, source_id in PROVINCE_QUERIES:
        if args.province and province != args.province:
            continue
        n = process_province(province, queries, source_id, dry_run=args.dry_run)
        total += n
        time.sleep(1)

    print(f"\n🎉 完成，共导入 {total} 条省级政策")


if __name__ == "__main__":
    main()
