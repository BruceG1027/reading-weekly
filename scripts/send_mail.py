#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把最新一期读书周报发到邮箱。

只用 Python 标准库，不需要 pip install。
凭据一律从环境变量读取，绝不写进仓库文件。

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

CSS = """
body{margin:0;padding:24px;background:#f6f7f9;}
.wrap{max-width:760px;margin:0 auto;background:#fff;border:1px solid #e5e7eb;
      border-radius:10px;padding:28px 32px;color:#1f2328;
      font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
      font-size:14px;line-height:1.7;}
h1{font-size:22px;margin:0 0 18px;padding-bottom:12px;border-bottom:2px solid #2f6feb;}
h2{font-size:17px;margin:26px 0 10px;padding-left:10px;border-left:3px solid #2f6feb;}
h3{font-size:15px;margin:18px 0 8px;}
p{margin:6px 0;}
blockquote{margin:10px 0;padding:8px 12px;background:#fff8e6;border-left:3px solid #e0a800;color:#6b5310;}
table{border-collapse:collapse;width:100%;margin:10px 0 18px;font-size:13px;}
th,td{border:1px solid #e5e7eb;padding:7px 9px;text-align:left;vertical-align:top;}
th{background:#f3f5f8;font-weight:600;}
tr:nth-child(even) td{background:#fafbfc;}
a{color:#2f6feb;text-decoration:none;}
code{background:#f1f3f5;padding:1px 5px;border-radius:3px;font-size:12px;}
.foot{margin-top:26px;padding-top:14px;border-top:1px solid #e5e7eb;color:#7a828e;font-size:12px;}
"""


def is_table_separator(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and bool(re.fullmatch(r"[|:\-\s]+", s))


def inline_md(text: str) -> str:
    """处理行内 Markdown：链接、行内代码，其余转义。"""
    links = []

    def hold(match):
        links.append((match.group(1), match.group(2)))
        return f"\x00L{len(links) - 1}\x00"

    out = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", hold, text)
    out = html.escape(out, quote=False)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    for i, (label, url) in enumerate(links):
        anchor = f'<a href="{html.escape(url, quote=True)}">{html.escape(label, quote=False)}</a>'
        out = out.replace(f"\x00L{i}\x00", anchor)
    return out


def md_to_html(md: str, repo_url: str) -> str:
    lines = md.splitlines()
    parts = []
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if line.startswith("|") and i + 1 < len(lines) and is_table_separator(lines[i + 1]):
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            i += 2
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            th = "".join(f"<th>{inline_md(c)}</th>" for c in header)
            body = "".join(
                "<tr>" + "".join(f"<td>{inline_md(c)}</td>" for c in row) + "</tr>"
                for row in rows
            )
            parts.append(f"<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>")
            continue
        if not line.strip():
            i += 1
            continue
        if line.startswith("### "):
            parts.append(f"<h3>{inline_md(line[4:])}</h3>")
        elif line.startswith("## "):
            parts.append(f"<h2>{inline_md(line[3:])}</h2>")
        elif line.startswith("# "):
            parts.append(f"<h1>{inline_md(line[2:])}</h1>")
        elif line.startswith("> "):
            parts.append(f"<blockquote>{inline_md(line[2:])}</blockquote>")
        else:
            parts.append(f"<p>{inline_md(line)}</p>")
        i += 1

    foot = ""
    if repo_url:
        base = repo_url.rstrip("/")
        foot = (
            '<div class="foot">'
            f'<a href="{html.escape(base + "/blob/main/LATEST.md", quote=True)}">在线查看完整周报</a>'
            f'　·　<a href="{html.escape(base + "/blob/main/reports", quote=True)}">历史周报</a>'
            "<br>由 GitHub Actions 每周一 09:00（UTC+8）生成并发送"
            "</div>"
        )
    return (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>读书周报</title><style>{CSS}</style></head>"
        f'<body><div class="wrap">{"".join(parts)}{foot}</div></body></html>'
    )


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
