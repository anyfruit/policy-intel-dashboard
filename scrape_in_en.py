#!/usr/bin/env python3
"""
scrape_in_en.py
抓取国际储能网"全国储能政策汇总"月报，提取各省政策条目入库。

来源: https://mchuneng.in-en.com (可访问，内容覆盖 22-25+ 省市)
数据: 每月一期，每期 40-90 条省级政策

用法:
    python scrape_in_en.py               # 抓取所有已知月报
    python scrape_in_en.py --dry-run     # 打印但不写库
    python scrape_in_en.py --url URL     # 只抓一个 URL
"""

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent))
from db import init_db, upsert_item_with_version
from tags import extract_tags, infer_categories, extract_impact_on, infer_status, infer_level

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

SOURCE_ID   = "inen_monthly"
SOURCE_NAME = "国际储能网-月度政策汇总"

# 省份识别
PROVINCES = [
    "安徽", "福建", "广东", "广西", "甘肃", "贵州", "海南", "河北", "河南",
    "黑龙江", "湖北", "湖南", "吉林", "江苏", "江西", "辽宁", "内蒙古",
    "宁夏", "青海", "山东", "山西", "陕西", "上海", "四川", "天津",
    "新疆", "云南", "浙江", "重庆", "北京", "深圳", "西安", "西藏",
]

# 已知月报 URL（按时间排序，从新到旧）
MONTHLY_URLS = [
    ("2026-02", "https://mchuneng.in-en.com/html/chunengy-52708.shtml"),
    ("2026-01", "https://mchuneng.in-en.com/html/chunengy-52667.shtml"),
    ("2025-12", "https://mchuneng.in-en.com/html/chunengy-52014.shtml"),
    ("2025-10", "https://mchuneng.in-en.com/html/chunengy-50060.shtml"),
    ("2025-08", "https://chuneng.in-en.com/html/chunengy-48228.shtml"),
    ("2025-07", "https://mchuneng.in-en.com/html/chunengy-47280.shtml"),
    ("2025-06", "https://mchuneng.in-en.com/html/chunengy-46814.shtml"),
    ("2025-03", "https://mchuneng.in-en.com/html/chunengy-43737.shtml"),
    ("2025-02", "https://mchuneng.in-en.com/html/chunengy-43065.shtml"),
    ("2025-01", "https://s.in-en.com/show/170/"),
]


def detect_province(text: str) -> str:
    """从文本中推断省份"""
    for p in PROVINCES:
        if text.startswith(p) or f"，{p}" in text[:20] or f" {p}" in text[:20]:
            return p
    return "全国"


def extract_items_from_article(html: str, year_month: str) -> list[dict]:
    """从月报 HTML 中提取政策条目"""
    soup = BeautifulSoup(html, "html.parser")
    art = soup.select_one(".article,.artContent,#artcontent,.news-content,article")
    if not art:
        art = soup.find("body")

    items = []
    year = year_month[:4]

    # 按段落解析：常见格式：
    #  - 2026年02月03日，...
    #  - 2月3日，...
    date_re = re.compile(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日[，,、\s]*(.+)")

    for p in art.select("p,li"):
        text = p.get_text(strip=True)
        if len(text) < 10:
            continue

        m = date_re.match(text)
        if not m:
            continue

        yy = m.group(1) or year
        mo, day, rest = m.group(2), m.group(3), m.group(4).strip()
        rest = re.sub(r"\s+", " ", rest)[:500]

        # 推断省份
        region = detect_province(rest)

        # 过滤：只收省级政策（跳过国家层面重复内容，已有其他来源）
        # 国家部委通常不会以省名开头
        if region == "全国" and not any(kw in rest for kw in ["储能", "电化学", "调峰", "辅助服务", "新能源"]):
            continue

        date_str = f"{int(yy):04d}-{int(mo):02d}-{int(day):02d}"

        # 构造 URL（月报页面本身作为来源，因为原始政策 URL 不在此）
        url_hash = hashlib.sha256(f"{date_str}:{rest[:100]}".encode()).hexdigest()
        fake_url = f"https://mchuneng.in-en.com/policy/{url_hash[:12]}"

        items.append({
            "url": fake_url,
            "title": rest[:200],
            "summary": text[:400],
            "date": date_str,
            "source_id": SOURCE_ID,
            "source_name": SOURCE_NAME,
            "region": region,
            # 注意：db.upsert_* 内部会 json.dumps，这里必须传 list
            "categories": infer_categories(rest),
            "tags": extract_tags(rest),
            "impact_on": extract_impact_on(rest),
            "level": "省" if region != "全国" else "国家",
            "status": infer_status(rest),
        })

    return items


def scrape_url(url: str, year_month: str, dry_run: bool = False) -> int:
    print(f"  📥 {year_month}: {url}")
    try:
        r = requests.get(url, headers=HEADERS, timeout=12)
        r.encoding = "utf-8"
        if r.status_code != 200:
            print(f"     ⚠️ HTTP {r.status_code}")
            return 0
    except Exception as e:
        print(f"     ❌ {e}")
        return 0

    items = extract_items_from_article(r.text, year_month)
    print(f"     找到 {len(items)} 条政策")

    if dry_run:
        for it in items[:5]:
            print(f"     [{it['region']}] {it['date']} {it['title'][:50]}")
        return len(items)

    imported = 0
    for item in items:
        try:
            upsert_item_with_version(item)
            imported += 1
        except Exception as e:
            print(f"     ⚠️ 入库失败: {e}")
            break

    print(f"     ✅ 入库 {imported}/{len(items)}")
    return imported


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--url", help="只抓单个 URL")
    p.add_argument("--ym", help="URL 对应的年月 YYYY-MM（与 --url 配合）", default="2025-01")
    args = p.parse_args()

    init_db()
    total = 0

    if args.url:
        total = scrape_url(args.url, args.ym, dry_run=args.dry_run)
    else:
        for ym, url in MONTHLY_URLS:
            n = scrape_url(url, ym, dry_run=args.dry_run)
            total += n
            time.sleep(0.8)

    print(f"\n🎉 完成，共处理 {total} 条省级政策")


if __name__ == "__main__":
    main()
