"""notifier.py — 邮件通知（可选）

server.py 用到：
- smtp_configured()
- send_email(to, subject, html_body)
- build_digest_html(sections, period_label)

如果没有配置 SMTP，则 smtp_configured=False，相关功能在 UI/接口上自动降级。
"""

from __future__ import annotations

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List


def smtp_configured() -> bool:
    host = (os.environ.get("SMTP_HOST") or "").strip()
    user = (os.environ.get("SMTP_USER") or "").strip()
    pw = (os.environ.get("SMTP_PASS") or "").strip()
    return bool(host and user and pw)


def send_email(to: str, subject: str, html_body: str) -> None:
    if not smtp_configured():
        raise RuntimeError("SMTP 未配置")

    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT") or 587)
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS")
    from_addr = (os.environ.get("SMTP_FROM") or user).strip()

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to
    msg.attach(MIMEText(html_body or "", "html", "utf-8"))

    with smtplib.SMTP(host, port, timeout=20) as s:
        s.starttls()
        s.login(user, pw)
        s.sendmail(from_addr, [to], msg.as_string())


def build_digest_html(sections: List[dict], period_label: str = "每日摘要") -> str:
    parts = [
        f"<h2>⚡ 储能政策情报 · {period_label}</h2>",
    ]
    for sec in sections or []:
        name = sec.get("subscription_name") or sec.get("subscription_name") or "订阅"
        items = sec.get("items") or []
        parts.append(f"<h3>{name}（{len(items)}条）</h3>")
        if not items:
            parts.append("<p style='color:#64748b'>无匹配内容</p>")
            continue
        parts.append("<ul>")
        for it in items[:50]:
            title = (it.get("title") or "").replace("<", "&lt;").replace(">", "&gt;")
            url = it.get("url") or ""
            date = (it.get("date") or "")[:10]
            region = it.get("region") or ""
            meta = " · ".join([x for x in [date, region] if x])
            parts.append(f"<li><a href='{url}' target='_blank' rel='noreferrer'>{title}</a> <span style='color:#94a3b8;font-size:12px'>({meta})</span></li>")
        parts.append("</ul>")

    parts.append("<hr/><p style='color:#94a3b8;font-size:12px'>本邮件由 Policy Intel Dashboard 自动生成。</p>")
    return "\n".join(parts)
