#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把最新一期读书周报发到邮箱。

只用 Python 标准库，不需要 pip install。
凭据一律从环境变量读取，绝不写进仓库文件。

邮件正文按「手机微信里打开 QQ 邮箱」的场景设计：
  * 不用多列数据表格（窄屏会被压成竖排），改成纵向卡片列表
  * 样式全部内联，不依赖 <style> 和媒体查询（部分邮件客户端会剥离）
  * 外层只有一层 table 做居中容器，内容层宽度 100% 自适应
  * 优先读 data/weekly.json 渲染；没有时回退到解析 LATEST.md

环境变量：
  MAIL_USER           发件邮箱（必填）
  MAIL_PASSWORD       邮箱授权码 / 应用专用密码（必填）
  MAIL_TO             收件邮箱，多个用逗号分隔；留空则发给自己
  MAIL_SMTP_HOST      SMTP 服务器，通常会自动按邮箱后缀推断（可选）
  MAIL_SMTP_PORT      端口，默认 465（可选）
  MAIL_SMTP_SECURITY  ssl / starttls / none，默认自动推断（可选）
  REPO_URL            仓库地址，用于邮件里的「在线查看」链接（可选）

用法：
  python scripts/send_mail.py            发送邮件
  python scripts/send_mail.py --dry-run  只生成 mail-preview.html，不发送
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, make_msgid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LATEST = ROOT / "LATEST.md"
PREVIEW = ROOT / "mail-preview.html"
WEEKLY_JSON = ROOT / "data" / "weekly.json"
CST = timezone(timedelta(hours=8))

# 常见邮箱的 SMTP 设置，省得手动填
SMTP_PRESETS = {
    "qq.com": ("smtp.qq.com", 465, "ssl"),
    "foxmail.com": ("smtp.qq.com", 465, "ssl"),
    "163.com": ("smtp.163.com", 465, "ssl"),
    "126.com": ("smtp.126.com", 465, "ssl"),
    "yeah.net": ("smtp.yeah.net", 465, "ssl"),
    "sina.com": ("smtp.sina.com", 465, "ssl"),
    "sina.cn": ("smtp.sina.cn", 465, "ssl"),
    "139.com": ("smtp.139.com", 465, "ssl"),
    "gmail.com": ("smtp.gmail.com", 465, "ssl"),
    "outlook.com": ("smtp-mail.outlook.com", 587, "starttls"),
    "hotmail.com": ("smtp-mail.outlook.com", 587, "starttls"),
    "live.com": ("smtp-mail.outlook.com", 587, "starttls"),
}

FONT = "-apple-system,BlinkMacSystemFont,Segoe UI,PingFang SC,Microsoft YaHei,Hiragino Sans GB,sans-serif"
INK = "#1a1d21"
SUB = "#6b7280"
FAINT = "#9aa1ad"
LINE = "#eceff3"
BLUE = "#2f6feb"
LINK = "#1a73e8"
HIGHLIGHT = "#c2410c"


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def anchor(url: str, label: str, style: str = "") -> str:
    if not url:
        return esc(label)
    return f'<a href="{esc(url)}" style="{style}">{esc(label)}</a>'


def score_text(value) -> str:
    """豆瓣是 10 分制评分，微信读书是推荐值百分比，分开标注避免混淆。"""
    s = str(value or "").strip()
    if not s or s in ("—", "-"):
        return ""
    return f"推荐 {s}" if s.endswith("%") else f"评分 {s}"


def page_open() -> str:
    return (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="format-detection" content="telephone=no">'
        '<title>读书周报</title></head>'
        '<body style="margin:0;padding:0;background:#f2f4f7;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="background:#f2f4f7;border-collapse:collapse;table-layout:fixed;"><tr>'
        '<td style="padding:12px 8px 24px;text-align:left;">'
        '<div style="width:100%;max-width:600px;margin:0 auto;box-sizing:border-box;'
        f'background:#ffffff;border-radius:14px;padding:20px 16px 22px;'
        f'font-family:{FONT};color:{INK};font-size:14px;line-height:1.6;'
        f'text-align:left;">'
    )


def page_close() -> str:
    return "</div></td></tr></table></body></html>"


def header(week: str, generated_at: str, counts: dict) -> str:
    return (
        f'<div style="font-size:20px;font-weight:700;line-height:1.4;letter-spacing:.2px;">'
        f'读书周报 {esc(week)}</div>'
        f'<div style="font-size:12px;color:{FAINT};margin-top:6px;line-height:1.6;">'
        f'{esc(generated_at)} · 热门 {int(counts.get("hot", 0))} 本 · '
        f'新书 {int(counts.get("new", 0))} 本</div>'
        f'<div style="height:1px;background:{LINE};margin:16px 0 2px;"></div>'
    )


def section(title: str, margin_top: int = 24) -> str:
    return (
        f'<div style="margin:{margin_top}px 0 8px;padding-left:9px;'
        f'border-left:3px solid {BLUE};font-size:16px;font-weight:700;line-height:1.4;">'
        f'{esc(title)}</div>'
    )


def pick_card(title: str, book: dict, tone: str, index: str) -> str:
    """本周一本的大卡片：经典池暖色、年度池蓝色，只用内联样式。"""
    themes = {
        "warm": ("#fdf9f0", "#f0e5cd", "#9a7b3f", "#8a6d3b"),
        "blue": ("#f3f8ff", "#dce8fb", "#4a76bd", "#4a76bd"),
    }
    bg, border, index_color, badge_color = themes[tone]
    rank = f" · 第 {book.get('rank')} 位" if book.get("rank") else ""
    badge = f"{book.get('source', '')}{rank}".strip(" ·")
    meta = " · ".join(x for x in [book.get("author", ""), book.get("year", "")] if x)
    return (
        f'<div style="margin-top:{14 if index == "01" else 12}px;">'
        f'<div style="font-size:13px;font-weight:700;line-height:1.5;">{esc(title)}</div>'
        f'<div style="margin-top:8px;background:{bg};border:1px solid {border};'
        f'border-radius:12px;padding:14px 14px 13px;">'
        f'<div style="font-size:11px;font-weight:700;color:{index_color};letter-spacing:1px;">'
        f'{index}</div>'
        + anchor(
            book.get("url", ""), book.get("title", ""),
            f"display:block;margin-top:7px;font-size:19px;font-weight:700;line-height:1.45;"
            f"color:{INK};text-decoration:none;word-break:break-word;",
        )
        + (f'<div style="font-size:13px;color:{SUB};margin-top:6px;line-height:1.6;">'
           f'{esc(meta)}</div>' if meta else "")
        + (f'<div style="margin-top:9px;font-size:11px;line-height:1.6;color:{badge_color};">'
           f'{esc(badge)}</div>' if badge else "")
        + "</div></div>"
    )


def book_row(index: int, row: dict) -> str:
    score = score_text(row.get("score"))
    score_html = (
        f'<span style="color:{HIGHLIGHT};font-weight:600;">{esc(score)}</span>'
        if score else ""
    )
    meta = [x for x in [
        row.get("author", ""), row.get("year", ""),
        score_html, row.get("source", ""),
    ] if x]
    meta_html = " · ".join(meta)
    return (
        f'<div style="padding:10px 0;border-bottom:1px solid {LINE};">'
        f'<div style="font-size:15px;line-height:1.55;word-break:break-word;">'
        f'<span style="display:inline-block;min-width:20px;margin-right:6px;'
        f'font-size:12px;font-weight:600;color:{FAINT};">{index:02d}</span>'
        + anchor(
            row.get("url", ""), row.get("title", ""),
            f"color:{LINK};text-decoration:none;font-weight:600;",
        )
        + "</div>"
        + (f'<div style="font-size:12.5px;color:{SUB};line-height:1.6;'
           f'margin-top:4px;padding-left:26px;word-break:break-word;'
           f'overflow-wrap:anywhere;">{meta_html}</div>'
           if meta_html else "")
        + "</div>"
    )


def compact_row(title: str, meta: str = "", url: str = "") -> str:
    return (
        f'<div style="padding:9px 0;border-bottom:1px solid {LINE};">'
        f'<div style="font-size:14px;font-weight:600;line-height:1.5;word-break:break-word;">'
        + anchor(url, title, f"color:{INK};text-decoration:none;")
        + "</div>"
        + (f'<div style="font-size:12px;color:{FAINT};margin-top:2px;line-height:1.6;">'
           f'{esc(meta)}</div>' if meta else "")
        + "</div>"
    )


def as_count(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def status_block(status: list) -> str:
    rows = "".join(
        f'<div style="font-size:12px;color:{SUB};line-height:1.9;">'
        f'{esc(r.get("name", ""))}'
        f'<span style="color:{FAINT};"> · {as_count(r.get("count", 0))} 条 · '
        f'{esc(r.get("state", ""))}</span></div>'
        for r in status
    )
    return (
        f'<div style="margin-top:22px;background:#f7f9fc;border-radius:10px;'
        f'padding:12px 14px;">'
        f'<div style="font-size:13px;font-weight:700;">数据状态</div>'
        f'<div style="margin-top:6px;">{rows}</div></div>'
    )


def footer(repo_url: str) -> str:
    links = ""
    if repo_url:
        base = repo_url.rstrip("/")
        links = (
            anchor(base + "/blob/main/LATEST.md", "在线查看完整周报",
                   f"color:{LINK};text-decoration:none;")
            + "　·　"
            + anchor(base + "/blob/main/reports", "历史周报",
                     f"color:{LINK};text-decoration:none;")
            + "<br>"
        )
    return (
        f'<div style="margin-top:22px;padding-top:14px;border-top:1px solid {LINE};'
        f'font-size:11.5px;color:{FAINT};line-height:1.8;">'
        f'{links}订阅 feed.xml · 原始数据 data/snapshots/'
        f'<br>每周一 09:00（UTC+8）生成并发送</div>'
    )


def render_email(data: dict, repo_url: str) -> str:
    parts = [page_open(), header(data.get("week", ""), data.get("generated_at", ""),
                                 data.get("counts", {}))]
    if data.get("used_snapshot"):
        parts.append(
            f'<div style="margin-top:12px;padding:9px 12px;background:#fff8e6;'
            f'border-left:3px solid #e0a800;font-size:12px;color:#6b5310;">'
            f'本周全部数据源未取到，以下内容来自上一次快照。</div>'
        )

    picks = data.get("picks") or {}
    if picks.get("classic"):
        parts.append(pick_card("经典池 · 本周一本", picks["classic"], "warm", "01"))
    if picks.get("annual"):
        parts.append(pick_card("年度池 · 本周一本", picks["annual"], "blue", "02"))

    for key, title in (("hot", "本周热门"), ("new", "新书速递")):
        rows = data.get(key) or []
        if not rows:
            continue
        parts.append(section(title))
        parts.extend(book_row(i, r) for i, r in enumerate(rows, 1))

    for group in data.get("nyt") or []:
        parts.append(section(group.get("name", "NYT 畅销榜"), margin_top=20))
        for i, b in enumerate(group.get("books") or [], 1):
            parts.append(book_row(i, {
                "title": b.get("title", ""), "author": b.get("author", ""),
                "year": f"上榜 {b.get('weeks', 0)} 周", "url": b.get("url", ""),
                "source": group.get("name", ""),
            }))

    lists = data.get("annual_lists") or []
    if lists:
        parts.append(section("年度榜单"))
        due = set(data.get("due") or [])
        for item in lists:
            months = "/".join(str(m) for m in item.get("months", []))
            flag = " · 本月发布" if item.get("name") in due else ""
            parts.append(compact_row(
                item.get("name", ""),
                f"{item.get('org', '')} · {months} 月发布 · 始于 {item.get('since', '')}{flag}",
                item.get("url", ""),
            ))

    if data.get("status"):
        parts.append(status_block(data["status"]))
    parts.append(footer(repo_url))
    parts.append(page_close())
    return "".join(parts)


# ---- 回退路径：没有 weekly.json 时，直接从 Markdown 生成同样的卡片布局 ----

def is_table_separator(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and bool(re.fullmatch(r"[|:\-\s]+", s))


def inline_md(text: str) -> str:
    links = []

    def hold(match):
        links.append((match.group(1), match.group(2)))
        return f"\x00L{len(links) - 1}\x00"

    out = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", hold, text or "")
    out = html.escape(out, quote=False)
    out = re.sub(r"`([^`]+)`", r"\1", out)
    for i, (label, url) in enumerate(links):
        out = out.replace(f"\x00L{i}\x00", anchor(url, label, f"color:{LINK};text-decoration:none;"))
    return out


def md_link(text: str):
    match = re.fullmatch(r"\[([^\]]+)\]\(([^)\s]+)\)", (text or "").strip())
    if match:
        return match.group(1), match.group(2)
    return re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text or ""), ""


def fallback_table(header: list, rows: list) -> str:
    """把 Markdown 表格转成纵向卡片，绝不在窄屏输出多列表格。"""
    idx = {name.strip(): i for i, name in enumerate(header)}

    def get(row, name):
        i = idx.get(name)
        return row[i].strip() if i is not None and i < len(row) else ""

    if "书名" in idx:
        out = []
        for i, row in enumerate(rows, 1):
            title, url = md_link(get(row, "书名"))
            out.append(book_row(i, {
                "title": title, "url": url,
                "author": get(row, "作者"), "score": get(row, "评分"),
                "year": get(row, "出版年"), "source": get(row, "来源"),
            }))
        return "".join(out)

    title_col = "榜单" if "榜单" in idx else "数据源"
    meta_cols = [c for c in header if c != title_col]
    out = []
    for row in rows:
        title, url = md_link(get(row, title_col))
        meta = " · ".join(get(row, c) for c in meta_cols if get(row, c))
        out.append(compact_row(title, meta, url))
    return "".join(out)


def render_fallback(md: str, repo_url: str) -> str:
    lines = md.splitlines()
    parts = [page_open()]
    week = ""
    first_line = next((l for l in lines if l.startswith("# ")), "")
    week = first_line[2:].strip().replace("读书周报", "").strip()
    counts = {"hot": 0, "new": 0}
    meta = lines[2] if len(lines) > 2 else ""
    m_hot = re.search(r"热门\s*(\d+)", meta)
    m_new = re.search(r"新书\s*(\d+)", meta)
    if m_hot:
        counts["hot"] = int(m_hot.group(1))
    if m_new:
        counts["new"] = int(m_new.group(1))
    generated = re.search(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})", meta)
    parts.append(header(week, generated.group(1) if generated else "", counts))

    i, current = 0, ""
    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip():
            i += 1
            continue
        if line.startswith("## "):
            current = line[3:].strip()
            # 「本周一本」单独做成大卡片
            if current.startswith("经典池") or current.startswith("年度池"):
                tone = "warm" if current.startswith("经典池") else "blue"
                number = "01" if tone == "warm" else "02"
                body = lines[i + 2].strip() if i + 2 < len(lines) else ""
                fields = [x.strip() for x in body.split("　|　")]
                title, url = md_link(fields[0] if fields else "")
                parts.append(pick_card(current, {
                    "title": title, "url": url,
                    "author": fields[1] if len(fields) > 1 else "",
                    "year": fields[2] if tone == "warm" and len(fields) > 2 else "",
                    "source": fields[3] if tone == "warm" and len(fields) > 3 else (
                        fields[2] if len(fields) > 2 else ""),
                }, tone, number))
                i += 3
                continue
            if current == "数据状态":
                i += 1
                continue
            parts.append(section(current))
        elif line.startswith("### "):
            parts.append(section(line[4:].strip(), margin_top=18))
        elif line.startswith("# "):
            pass
        elif line.startswith("|") and i + 1 < len(lines) and is_table_separator(lines[i + 1]):
            header_cells = [c.strip() for c in line.strip().strip("|").split("|")]
            i += 2
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            if current == "数据状态":
                parts.append(status_block([
                    {"name": r[0], "count": r[1] if len(r) > 1 else 0,
                     "state": r[2] if len(r) > 2 else ""}
                    for r in rows
                ]))
            else:
                parts.append(fallback_table(header_cells, rows))
            continue
        elif line.startswith("> "):
            parts.append(
                f'<div style="margin-top:10px;padding:9px 12px;background:#fff8e6;'
                f'border-left:3px solid #e0a800;font-size:12px;color:#6b5310;">'
                f'{inline_md(line[2:])}</div>'
            )
        elif not line.startswith("|"):
            parts.append(
                f'<div style="font-size:12px;color:{FAINT};margin-top:8px;line-height:1.7;">'
                f'{inline_md(line)}</div>'
            )
        i += 1
    parts.append(footer(repo_url))
    parts.append(page_close())
    return "".join(parts)


def md_to_html(md: str, repo_url: str) -> str:
    try:
        data = json.loads(WEEKLY_JSON.read_text(encoding="utf-8"))
        title = re.search(r"^# 读书周报 (\S+)", md, re.M)
        same_week = bool(title) and title.group(1) == data.get("week")
        if same_week and (data.get("hot") or data.get("new")):
            return render_email(data, repo_url)
    except (OSError, ValueError):
        pass
    return render_fallback(md, repo_url)


def md_to_text(md: str, repo_url: str) -> str:
    out = []
    for line in md.splitlines():
        if is_table_separator(line):
            continue
        line = re.sub(r"\[([^\]]+)\]\([^)\s]+\)", r"\1", line)
        out.append(line.replace("`", ""))
    if repo_url:
        out += ["", "在线查看：" + repo_url.rstrip("/") + "/blob/main/LATEST.md"]
    return "\n".join(out)


def smtp_settings(user: str):
    domain = user.rsplit("@", 1)[-1].lower() if "@" in user else ""
    host, port, security = SMTP_PRESETS.get(domain, ("", 465, "ssl"))
    host = os.environ.get("MAIL_SMTP_HOST", "").strip() or host
    port = int(os.environ.get("MAIL_SMTP_PORT", "").strip() or port)
    security = os.environ.get("MAIL_SMTP_SECURITY", "").strip().lower() or security
    if not host:
        raise RuntimeError(
            f"无法自动识别 {domain} 的 SMTP 服务器，请设置环境变量 MAIL_SMTP_HOST（并按需设置 MAIL_SMTP_PORT / MAIL_SMTP_SECURITY）"
        )
    return host, port, security


def build_message(md: str, week: str, sender: str, recipients: list, repo_url: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = f"读书周报 {week}"
    msg["From"] = formataddr(("读书周报", sender))
    msg["To"] = ", ".join(recipients)
    msg["Date"] = format_datetime(datetime.now(CST))
    msg["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1] if "@" in sender else "localhost")
    msg.set_content(md_to_text(md, repo_url))
    msg.add_alternative(md_to_html(md, repo_url), subtype="html")
    return msg


def deliver(msg: EmailMessage, sender: str, password: str, host: str, port: int, security: str):
    # 明文 SMTP（一般只用于本地测试）不需要 TLS 上下文
    context = ssl.create_default_context() if security in ("ssl", "starttls") else None
    if security == "ssl":
        server = smtplib.SMTP_SSL(host, port, timeout=30, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
        server.ehlo()
        if security == "starttls":
            server.starttls(context=context)
            server.ehlo()
    with server:
        server.login(sender, password)
        server.send_message(msg)


def main():
    parser = argparse.ArgumentParser(description="把最新一期周报发到邮箱")
    parser.add_argument("--dry-run", action="store_true", help="只生成 mail-preview.html，不真正发送")
    parser.add_argument("--week", default="", help="覆盖周标识，默认从 LATEST.md 标题读取")
    args = parser.parse_args()

    if not LATEST.exists():
        raise SystemExit("找不到 LATEST.md，请先运行 python scripts/build_report.py")
    md = LATEST.read_text(encoding="utf-8")
    match = re.search(r"^# 读书周报 (\S+)", md, re.M)
    week = args.week or (match.group(1) if match else datetime.now(CST).strftime("%G-W%V"))

    sender = os.environ.get("MAIL_USER", "").strip()
    password = os.environ.get("MAIL_PASSWORD", "").strip()
    to_raw = os.environ.get("MAIL_TO", "").strip() or sender
    recipients = [x.strip() for x in to_raw.split(",") if x.strip()]
    repo_url = os.environ.get("REPO_URL", "").strip()

    if args.dry_run:
        doc = md_to_html(md, repo_url or "https://github.com/example/reading")
        PREVIEW.write_text(doc, encoding="utf-8")
        print(f"dry-run：已生成 {PREVIEW.name}（未发送邮件）")
        print("渲染来源：", "data/weekly.json" if WEEKLY_JSON.exists() else "LATEST.md 回退")
        print("正文预览：", md_to_text(md, "")[:180].replace("\n", " / "))
        return

    if not sender or not password:
        print("邮件：未配置 MAIL_USER / MAIL_PASSWORD，跳过发送")
        print("      周报已正常生成，其他功能不受影响")
        return
    if not recipients:
        recipients = [sender]

    msg = build_message(md, week, sender, recipients, repo_url)
    try:
        host, port, security = smtp_settings(sender)
        print(f"邮件：正在通过 {host}:{port} ({security}) 发送给 {', '.join(recipients)}")
        deliver(msg, sender, password, host, port, security)
        print(f"邮件：已发送「读书周报 {week}」")
    except smtplib.SMTPAuthenticationError as exc:
        raise SystemExit(
            "邮件发送失败：邮箱账号或授权码不正确。\n"
            "  1. 确认用的是「授权码 / 应用专用密码」，不是邮箱登录密码；\n"
            "  2. 确认已在邮箱设置里开启 IMAP/SMTP 服务；\n"
            f"  3. 服务端返回：{exc}"
        )
    except Exception as exc:
        raise SystemExit(f"邮件发送失败：{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
