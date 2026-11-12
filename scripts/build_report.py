#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""读书周报生成器。

数据源：
  豆瓣月度热门榜网页 + 豆瓣榜单官方接口
  微信读书榜单页内嵌数据（飙升榜 / 神作榜 / 总榜 / 新书榜）
  备源：RSSHub 的 RSS 输出
  可选：NYT Books API（需要 NYT_API_KEY）

产出：
  data/snapshots/<ISO周>.json   原始快照
  data/weekly.json              最新一期结构化数据（邮件渲染用）
  data/picks-history.json       每周两本的抽取记录，保证不重复
  reports/<ISO周>.md            周报
  LATEST.md                     最新一期
  feed.xml                      RSS 订阅源

只使用 Python 标准库，不需要 pip install。
"""
from __future__ import annotations

import argparse
import html as html_lib
import json
import math
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
REPORTS = ROOT / "reports"
SNAPSHOTS = DATA / "snapshots"
PICKS_HISTORY = DATA / "picks-history.json"
WEEKLY_JSON = DATA / "weekly.json"
DOUBAN_API = (
    "https://m.douban.com/rexxar/api/v2/subject_collection/{cid}/items"
    "?start=0&count={count}&mode=collection&for_mobile=1"
)
CHART_URL = "https://book.douban.com/chart"
WEREAD_URL = "https://weread.qq.com/web/category/{cid}"
WEREAD_MARKER = "window.__INITIAL_STATE__="
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
)
CST = timezone(timedelta(hours=8))


def read_json(path: Path, default=None):
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def write_json(path: Path, obj):
    write_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def http_get(url: str, referer: str = "", timeout: int = 15, retries: int = 2,
             user_agent: str = UA) -> bytes:
    headers = {"User-Agent": user_agent, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last = exc
            if attempt + 1 < retries:
                time.sleep(1.2)
    raise RuntimeError(str(last))


def cell(value) -> str:
    s = "" if value is None else str(value)
    return s.replace("|", "\\|").replace("\n", " ").replace("\r", " ").strip()


def strip_tags(text: str) -> str:
    return html_lib.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def search_link(title: str) -> str:
    return "https://book.douban.com/subject_search?search_text=" + urllib.parse.quote(title)


def normalize(item: dict, source: str) -> dict:
    rating = item.get("rating") or {}
    authors = item.get("author") or []
    subtitle = item.get("card_subtitle") or ""
    info = item.get("info") or ""
    # 新书速递的字段都在 card_subtitle：作者 / 年份 / 出版社
    parts = [p.strip() for p in subtitle.split(" / ") if p.strip()]
    author = ""
    if isinstance(authors, list) and authors:
        author = str(authors[0])
    if not author and parts:
        author = parts[0]
    if not author and info:
        author = info.split("/")[0].strip()
    year = item.get("year") or []
    press = item.get("press") or []
    year_text = year[0] if year else (parts[1] if len(parts) > 1 else "")
    press_text = press[0] if press else (parts[2] if len(parts) > 2 else "")
    # 评分人数不足时豆瓣返回 0，不显示成 0.0
    try:
        value = float(rating.get("value"))
        score = f"{value:.1f}" if value > 0 else ""
    except (TypeError, ValueError):
        score = ""
    return {
        "title": cell(item.get("title") or ""),
        "author": cell(author),
        "score": score,
        "year": cell(year_text),
        "press": cell(press_text),
        "url": item.get("url") or "",
        "type": item.get("type") or "",
        "source": source,
    }


def fetch_douban(src: dict) -> list:
    """榜单接口：新书速递返回 items，普通榜单返回 subject_collection_items。"""
    url = DOUBAN_API.format(cid=src["id"], count=max(int(src["count"]), 20))
    referer = f"https://m.douban.com/subject_collection/{src['id']}"
    raw = http_get(url, referer=referer, user_agent=MOBILE_UA)
    payload = json.loads(raw.decode("utf-8"))
    items = payload.get("subject_collection_items") or payload.get("items") or []
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") and item.get("type") != "book":
            continue
        row = normalize(item, src["name"])
        if row["title"]:
            out.append(row)
    return out[: int(src["count"])]


def fetch_chart(src: dict) -> list:
    """豆瓣月度热门图书榜（book.douban.com/chart），每月更新。"""
    page = http_get(CHART_URL, timeout=18).decode("utf-8", "replace")
    out = []
    for block in re.split(r'<li class="media clearfix">', page)[1:]:
        m = re.search(
            r'<h2[^>]*>\s*<a[^>]+href="(https://book\.douban\.com/subject/\d+/)"[^>]*>(.*?)</a>',
            block, re.S,
        )
        if not m:
            continue
        title = strip_tags(m.group(2))
        abstract = re.search(r'<p class="subject-abstract[^"]*">(.*?)</p>', block, re.S)
        parts = [p.strip() for p in strip_tags(abstract.group(1)).split("/")] if abstract else []
        score_m = re.search(r'<span class="font-small fleft">([\d.]+)</span>', block)
        out.append({
            "title": cell(title),
            "author": cell(parts[0] if parts else ""),
            "score": score_m.group(1) if score_m else "",
            "year": cell(parts[1] if len(parts) > 1 else ""),
            "press": cell(parts[2] if len(parts) > 2 else ""),
            "url": m.group(1),
            "type": "book",
            "source": src["name"],
        })
        if len(out) >= int(src["count"]):
            break
    return out


def fetch_weread(src: dict) -> list:
    """微信读书榜单：页面里 window.__INITIAL_STATE__ 已内嵌完整书单，无需登录。

    newRating 是「推荐值」（882 = 88.2%），newRatingCount 是点评人数。
    书籍详情链接用 deepLink 里的 v 参数拼 /web/reader/，已在浏览器验证可打开。
    """
    page = http_get(
        WEREAD_URL.format(cid=src["id"]),
        referer="https://weread.qq.com/",
        timeout=20,
    ).decode("utf-8", "replace")
    pos = page.find(WEREAD_MARKER)
    if pos < 0:
        raise RuntimeError("微信读书页面没有内嵌榜单数据")
    state, _ = json.JSONDecoder().raw_decode(page[pos + len(WEREAD_MARKER):])
    store = state.get("categoryStoreModule") or {}
    out = []
    for entry in store.get("categoryBookList") or []:
        book = entry.get("bookInfo") or {}
        title = (book.get("title") or "").strip()
        if not title:
            continue
        rating = book.get("newRating") or 0
        score = f"{rating / 10:.1f}%" if rating else ""
        deep = book.get("deepLink") or ""
        vid = re.search(r"[?&]v=([^&]+)", deep)
        url = (
            "https://weread.qq.com/web/reader/" + vid.group(1)
            if vid else WEREAD_URL.format(cid=src["id"])
        )
        out.append({
            "title": cell(title),
            "author": cell(book.get("author") or ""),
            "score": score,
            "year": cell((book.get("publishTime") or "")[:4]),
            "press": "",
            "url": url,
            "type": "book",
            "source": src["name"],
            "votes": int(book.get("newRatingCount") or 0),
            "category": cell(book.get("category") or ""),
        })
        if len(out) >= int(src["count"]):
            break
    return out


def title_key(title: str) -> str:
    """跨源去重用的书名指纹：去掉标点、空格、副标题括号内容。"""
    s = re.sub(r"[（(].*?[)）]", "", title or "")
    return re.sub(r"[\s\W_]+", "", s).lower()


def mix_sources(groups: list, limit: int) -> list:
    """各数据源按名次交错合并，保证每个源都能进榜；同名书只留第一次。"""
    out, seen = [], set()
    rounds = max((len(g) for g in groups), default=0)
    for i in range(rounds):
        for group in groups:
            if i >= len(group):
                continue
            row = group[i]
            key = title_key(row["title"])
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(row)
            if len(out) >= limit:
                return out
    return out


def fetch_rsshub(base: str, src: dict) -> list:
    """豆瓣官方接口不可用时，退到 RSSHub 的 RSS 输出。"""
    if src["section"] == "new":
        route = "/douban/book/latest/all"
    else:
        route = "/douban/book/rank/" + ("nonfiction" if "非虚构" in src["name"] else "fiction")
    raw = http_get(base.rstrip("/") + route, timeout=15, retries=1)
    root = ET.fromstring(raw)
    out = []
    for node in root.findall(".//item")[: int(src["count"])]:
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        if title:
            out.append({
                "title": cell(title.split("-")[0]),
                "author": "",
                "score": "",
                "year": "",
                "press": "",
                "url": link,
                "type": "book",
                "source": src["name"] + "（RSSHub）",
            })
    return out


def fetch_nyt(api_key: str, wanted: list) -> list:
    url = "https://api.nytimes.com/svc/books/v3/lists/overview.json?api-key=" + urllib.parse.quote(api_key)
    payload = json.loads(http_get(url, timeout=20).decode("utf-8"))
    lists = (payload.get("results") or {}).get("lists") or []
    out = []
    for group in lists:
        if group.get("list_name") not in wanted:
            continue
        books = [{
            "title": cell(b.get("title") or ""),
            "author": cell(b.get("author") or ""),
            "rank": b.get("rank"),
            "weeks": b.get("weeks_on_list"),
            "url": b.get("amazon_product_url") or "",
            "source": cell(group.get("list_name") or ""),
        } for b in (group.get("books") or [])]
        out.append({"name": cell(group.get("list_name") or ""), "books": books[:5]})
    return out


def collect(sources: dict, offline: bool = False):
    """返回 (hot, new, status_rows, nyt)。各源分别取数，再交错混合，避免单一平台占满版面。"""
    hot_groups, new_groups, status = [], [], []
    for src in sources["douban"]:
        items, state = [], "未取到"
        if offline:
            state = "离线"
        elif src["id"] == "chart":
            try:
                items = fetch_chart(src)
                state = "官方网页"
            except Exception:
                state = "网页解析失败"
        else:
            try:
                items = fetch_douban(src)
                state = "官方接口"
            except Exception:
                for base in sources.get("rsshub_fallback", []):
                    try:
                        items = fetch_rsshub(base, src)
                        state = "RSSHub"
                        break
                    except Exception:
                        continue
        if not items and state in ("官方接口", "官方网页"):
            state = "接口无图书条目"
        status.append({"name": src["name"], "count": len(items), "state": state})
        if items:
            (hot_groups if src["section"] == "hot" else new_groups).append(items)
        time.sleep(0.6)

    for src in sources.get("weread", []):
        items, state = [], "未取到"
        if offline:
            state = "离线"
        else:
            try:
                items = fetch_weread(src)
                state = "榜单页内嵌数据"
            except Exception:
                state = "榜单页解析失败"
        status.append({"name": src["name"], "count": len(items), "state": state})
        if items:
            (hot_groups if src["section"] == "hot" else new_groups).append(items)
        time.sleep(0.5)

    hot = mix_sources(hot_groups, int(sources.get("hot_limit", 16)))
    new = mix_sources(new_groups, int(sources.get("new_limit", 12)))

    nyt = []
    api_key = os.environ.get("NYT_API_KEY", "").strip()
    if api_key and not offline:
        try:
            nyt = fetch_nyt(api_key, sources.get("nyt_lists", []))
            status.append({"name": "NYT 畅销榜", "count": sum(len(g["books"]) for g in nyt), "state": "官方 API"})
        except Exception:
            status.append({"name": "NYT 畅销榜", "count": 0, "state": "请求失败"})
    else:
        status.append({"name": "NYT 畅销榜", "count": 0, "state": "未配置 key"})
    return hot, new, status, nyt


def dedup(rows: list) -> list:
    seen, out = set(), []
    for r in rows:
        key = r.get("url") or r.get("title")
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def latest_snapshot():
    files = sorted(SNAPSHOTS.glob("*.json"))
    return read_json(files[-1]) if files else None


def pool_by_quality(name: str, books: list) -> list:
    """经典池按百大位次排序（无位次的中文经典补充排后），年度池按编辑推荐顺序。"""
    scored = []
    for i, book in enumerate(books):
        rank = book.get("rank")
        quality = 1000 - int(rank) if name == "classic" and rank else (
            500 - i if name == "classic" else 100 - i
        )
        scored.append((quality, book))
    scored.sort(key=lambda pair: -pair[0])
    return [book for _, book in scored]


def find_book(books: list, title: str):
    return next((b for b in books if b.get("title") == title), None)


def choose_pick(name: str, books: list, history: dict, week: str, exclude=()) -> dict | None:
    ordered = pool_by_quality(name, books)
    used = set(history["used"].get(name, []))
    candidates = [b for b in ordered if b["title"] not in used and b["title"] not in exclude]
    if not candidates:
        # 整池抽完一轮，清空记录重新开始
        history["used"][name] = []
        candidates = [b for b in ordered if b["title"] not in exclude]
    if not candidates:
        return None
    # 只从质量排序的前 60% 里抽，保证「高分高质量」
    top_n = max(3, math.ceil(len(candidates) * 0.6))
    pick = random.Random(f"{week}|{name}").choice(candidates[:top_n])
    history["used"][name].append(pick["title"])
    return pick


def pick_weekly(week: str, classics: list, annual_picks: list):
    """每周两本：高分段内确定性抽取、跨周不重复、两本互不相同、同周重跑结果一致。"""
    history = read_json(PICKS_HISTORY, {}) or {}
    history.setdefault("weeks", {})
    history.setdefault("used", {})
    for key in ("classic", "annual"):
        history["used"].setdefault(key, [])

    saved = history["weeks"].get(week)
    if saved:
        classic = find_book(classics, saved.get("classic", ""))
        annual = find_book(annual_picks, saved.get("annual", ""))
        if classic and annual and classic["title"] != annual["title"]:
            return classic, annual

    classic = choose_pick("classic", classics, history, week)
    annual = choose_pick(
        "annual", annual_picks, history, week,
        exclude={classic["title"]} if classic else set(),
    )
    history["weeks"][week] = {
        "classic": classic["title"] if classic else "",
        "annual": annual["title"] if annual else "",
    }
    for old in sorted(history["weeks"])[:-104]:
        history["weeks"].pop(old, None)
    write_json(PICKS_HISTORY, history)
    return classic, annual


def book_table(rows: list) -> str:
    if not rows:
        return "本周未取到数据。\n"
    lines = [
        "| # | 书名 | 作者 | 评分 | 出版年 | 来源 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for i, r in enumerate(rows, 1):
        title = f"[{r['title']}]({r['url']})" if r.get("url") else r["title"]
        lines.append(
            f"| {i} | {title} | {r['author']} | {r['score'] or '—'} | "
            f"{r['year'] or '—'} | {r['source']} |"
        )
    return "\n".join(lines) + "\n"


def render_markdown(week: str, now: datetime, hot: list, new: list, status: list,
                    nyt: list, classic, annual_pick, annual: dict,
                    used_snapshot: bool) -> str:
    lines = [
        f"# 读书周报 {week}",
        "",
        f"生成时间：{now.strftime('%Y-%m-%d %H:%M')} (UTC+8)　|　"
        f"热门 {len(hot)} 本　|　新书 {len(new)} 本",
        "",
    ]
    if used_snapshot:
        lines += ["> 本周全部数据源未取到，以下内容来自上一次快照。", ""]

    # 一、经典池 · 本周一本
    if classic:
        rank = f" · 第 {classic['rank']} 位" if classic.get("rank") else ""
        lines += [
            "## 经典池 · 本周一本", "",
            f"[{classic['title']}]({search_link(classic['title'])})　|　{classic['author']}　|　"
            f"{classic['year']}　|　{classic['source']}{rank}",
            "",
        ]
    # 二、年度池 · 本周一本
    if annual_pick:
        lines += [
            "## 年度池 · 本周一本", "",
            f"[{annual_pick['title']}]({search_link(annual_pick['title'])})　|　"
            f"{annual_pick['author']}　|　{annual_pick['source']}",
            "",
        ]

    # 三、四、榜单
    lines += ["## 本周热门", "", book_table(hot), "## 新书速递", "", book_table(new)]

    if nyt:
        lines += ["## NYT 畅销榜", ""]
        for group in nyt:
            lines += [f"### {group['name']}", ""]
            rows = [{
                "title": b["title"], "author": b["author"], "score": "",
                "year": f"上榜 {b['weeks']} 周", "url": b["url"], "source": group["name"],
            } for b in group["books"]]
            lines += [book_table(rows), ""]

    # 五、年度榜单
    lines += ["## 年度榜单", "", "| 榜单 | 主办 | 发布月份 | 起始 |", "| --- | --- | --- | --- |"]
    for item in annual["lists"]:
        name = f"[{item['name']}]({item['url']})" if item.get("url") else item["name"]
        months = "/".join(str(m) for m in item["months"])
        lines.append(f"| {name} | {item['org']} | {months} 月 | {item['since']} |")
    lines.append("")
    due = [x["name"] for x in annual["lists"] if now.month in x["months"]]
    if due:
        lines += [f"本月发布：{'、'.join(due)}。", ""]

    # 六、数据状态
    lines += ["## 数据状态", "", "| 数据源 | 条数 | 状态 |", "| --- | --- | --- |"]
    for row in status:
        lines.append(f"| {row['name']} | {row['count']} | {row['state']} |")
    lines += [
        "",
        "订阅地址：`feed.xml`　|　历史周报：`reports/`　|　原始数据：`data/snapshots/`",
        "",
    ]
    return "\n".join(lines)


def render_feed(repo_url: str) -> str:
    files = sorted(REPORTS.glob("*.md"), reverse=True)[:10]
    base = repo_url.rstrip("/")
    items = []
    for path in files:
        week = path.stem
        link = f"{base}/blob/main/reports/{week}.md" if base else f"reports/{week}.md"
        try:
            body = path.read_text(encoding="utf-8")
        except OSError:
            body = ""
        def plain(text: str) -> str:
            return re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text).strip().split("　")[0]

        meta = body.split("\n")[2].strip() if len(body.split("\n")) > 2 else ""
        classic_m = re.search(r"## 经典池[^\n]*\n\n([^\n]+)", body)
        annual_m = re.search(r"## (?:年度池|年度一本)[^\n]*\n\n([^\n]+)", body)
        pieces = [meta]
        if classic_m:
            pieces.append("经典 " + plain(classic_m.group(1)))
        if annual_m:
            pieces.append("年度 " + plain(annual_m.group(1)))
        summary = " | ".join(pieces)[:300]
        pub = datetime.fromtimestamp(path.stat().st_mtime, tz=CST)
        items.append(
            "<item>"
            f"<title>{xml_escape('读书周报 ' + week)}</title>"
            f"<link>{xml_escape(link)}</link>"
            f"<guid isPermaLink=\"false\">{xml_escape(week)}</guid>"
            f"<pubDate>{format_datetime(pub)}</pubDate>"
            f"<description>{xml_escape(summary)}</description>"
            "</item>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0"><channel>'
        "<title>读书周报</title>"
        f"<link>{xml_escape(base or 'LATEST.md')}</link>"
        "<description>每周热门书单、新书速递、年度榜单与经典书目</description>"
        "<language>zh-CN</language>"
        + "".join(items)
        + "</channel></rss>\n"
    )


def notify(week: str, hot: list, new: list, classic, annual_pick):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not (token and chat_id):
        print("notify: 未配置 Telegram，跳过")
        return
    text = "\n".join([
        f"读书周报 {week}",
        f"热门 {len(hot)} 本 / 新书 {len(new)} 本",
        f"经典：{classic['title'] if classic else '—'}",
        f"年度：{annual_pick['title'] if annual_pick else '—'}",
    ])
    payload = json.dumps({"chat_id": chat_id, "text": text, "disable_web_page_preview": True}).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print("notify: 已发送", resp.status)
    except Exception as exc:
        print("notify: 发送失败", exc)


def main():
    parser = argparse.ArgumentParser(description="生成读书周报")
    parser.add_argument("--week", default="", help="覆盖 ISO 周标识，如 2026-W46")
    parser.add_argument("--notify", action="store_true", help="生成后发送 Telegram 摘要")
    parser.add_argument("--offline", action="store_true", help="不联网，仅用现有数据生成")
    args = parser.parse_args()

    now = datetime.now(CST)
    week = args.week or now.strftime("%G-W%V")
    sources = read_json(ROOT / "scripts" / "sources.json", {"douban": [], "rsshub_fallback": [], "nyt_lists": []})
    classics = read_json(DATA / "classics.json", {"books": []})
    annual = read_json(DATA / "annual.json", {"lists": [], "picks": []})

    hot, new, status, nyt = collect(sources, offline=args.offline)
    used_snapshot = False
    if not hot and not new:
        snap = latest_snapshot()
        if snap:
            hot, new = snap.get("hot", []), snap.get("new", [])
            used_snapshot = True
            status.append({"name": "上期快照", "count": len(hot) + len(new), "state": "已回退"})

    if not args.offline and not used_snapshot:
        write_json(SNAPSHOTS / f"{week}.json", {
            "week": week,
            "generated_at": now.isoformat(),
            "hot": hot,
            "new": new,
            "nyt": nyt,
            "status": status,
        })

    classic, annual_pick = pick_weekly(
        week, classics.get("books", []), annual.get("picks", [])
    )
    md = render_markdown(
        week, now, hot, new, status, nyt, classic, annual_pick, annual, used_snapshot
    )
    write_text(REPORTS / f"{week}.md", md)
    write_text(ROOT / "LATEST.md", md)
    write_text(ROOT / "feed.xml", render_feed(os.environ.get("REPO_URL", "").strip()))

    # 结构化数据：邮件卡片渲染直接用它，不必再反解 Markdown
    due = [x["name"] for x in annual.get("lists", []) if now.month in x["months"]]
    write_json(WEEKLY_JSON, {
        "week": week,
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "counts": {"hot": len(hot), "new": len(new)},
        "used_snapshot": used_snapshot,
        "picks": {
            "classic": dict(classic, url=search_link(classic["title"])) if classic else None,
            "annual": dict(annual_pick, url=search_link(annual_pick["title"])) if annual_pick else None,
        },
        "hot": hot,
        "new": new,
        "nyt": nyt,
        "annual_lists": annual.get("lists", []),
        "due": due,
        "status": status,
    })

    print(f"周报 {week}：热门 {len(hot)} 本，新书 {len(new)} 本")
    print(f"  经典池：{classic['title'] if classic else '—'}")
    print(f"  年度池：{annual_pick['title'] if annual_pick else '—'}")
    for row in status:
        print(f"  {row['name']}: {row['count']} 条 / {row['state']}")
    if args.notify:
        notify(week, hot, new, classic, annual_pick)


if __name__ == "__main__":
    main()
