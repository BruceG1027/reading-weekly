# 读书周报

每周一 09:00（UTC+8）自动抓取书单，生成一份周报、一份 RSS、一份原始数据快照，并**发一封邮件到你的邮箱**。

> 第一次配置请直接看 **[开始使用.md](开始使用.md)** —— 从注册、上传到收信，全程点鼠标，不用记命令。

## 目录

| 路径 | 内容 |
| --- | --- |
| `开始使用.md` | 傻瓜式配置教程（含邮箱授权码、Secret、排错表） |
| `LATEST.md` | 最新一期周报 |
| `reports/` | 按 ISO 周归档的历史周报 |
| `feed.xml` | RSS 订阅源 |
| `data/snapshots/` | 每周抓取的原始数据（JSON） |
| `scripts/build_report.py` | 生成脚本，只用 Python 标准库 |
| `scripts/send_mail.py` | 发邮件脚本，只用 Python 标准库 |
| `scripts/sources.json` | 数据源与抓取数量 |
| `data/classics.json` | 经典池，每周从中抽一本 |
| `data/annual.json` | 年度榜单目录与年度代表作 |
| `.github/workflows/weekly.yml` | 每周定时任务 |

## 本地运行

```
python scripts/build_report.py                 # 生成周报
python scripts/send_mail.py --dry-run          # 预览邮件长什么样（不发送）
```

Windows 也可以双击 `run.bat`。常用参数：

| 参数 | 作用 |
| --- | --- |
| `--offline` | 不联网，用现有快照重新生成周报 |
| `--week 2026-W46` | 指定周标识 |
| `--notify` | 生成后发送 Telegram 摘要 |
| `send_mail.py --dry-run` | 生成 `mail-preview.html`，浏览器打开即可预览邮件 |
| `send_mail.py`（不带参数） | 真正发邮件，需先设置 `MAIL_USER` / `MAIL_PASSWORD` |

## 每周自动推送到邮箱

一句话概括：把整个文件夹推成 GitHub 仓库 → 开仓库写权限 → 拿邮箱授权码 → 填 3 个 Secret → 手动跑一次验证。详细步骤见 **[开始使用.md](开始使用.md)**。

工作流每次运行时依次做三件事：

1. `build_report.py --notify` 生成 `LATEST.md`、`reports/*.md`、`feed.xml`、快照 JSON
2. 把变更提交回仓库（需要 Workflow permissions 设为 **Read and write**）
3. `send_mail.py` 把 `LATEST.md` 渲染成 HTML 邮件发出

需要的 3 个 Secret（仓库 Settings → Secrets and variables → Actions）：

| Secret | 值 |
| --- | --- |
| `MAIL_USER` | 发件邮箱 |
| `MAIL_PASSWORD` | 邮箱**授权码**（不是登录密码） |
| `MAIL_TO` | 收件邮箱，留空则发给自己 |

SMTP 服务器按邮箱后缀自动推断（QQ / Foxmail / 163 / 126 / Gmail / Outlook 等内置）；特殊邮箱可用 `MAIL_SMTP_HOST`、`MAIL_SMTP_PORT`、`MAIL_SMTP_SECURITY` 覆盖。

**没配置 Secret 时发信步骤会安全跳过**，不影响周报生成；配置错了才会让任务标红，方便在 Actions 日志里看到原因。

订阅 RSS：在 RSS 阅读器里填 `https://raw.githubusercontent.com/<用户名>/<仓库名>/main/feed.xml`。

Telegram 推送（可选）：加 Secret `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID`。

NYT 畅销榜（可选）：在 [developer.nytimes.com](https://developer.nytimes.com/apis) 免费注册一个 API key，存成 Secret `NYT_API_KEY`。不配置时周报里这一栏显示「未配置 key」，其余功能不受影响。

> GitHub 对 60 天没有任何活动的仓库会暂停定时任务，偶尔手动跑一次即可恢复。定时任务是 UTC 时间，`0 1 * * 1` = 北京时间周一 09:00。

## 数据源

| 源 | 口径 | 更新 |
| --- | --- | --- |
| 豆瓣 `book.douban.com/chart` | 月度热门图书榜（解析网页，含评分） | 每月 |
| 豆瓣 `book_fiction` / `book_nonfiction` | 热门图书排行 | 每周一 |
| 豆瓣 `new_book_all` / `new_book_fiction` | 新书速递 | 每周 |
| RSSHub `/douban/book/rank/*`、`/douban/book/latest/*` | 官方接口失败时的备用 | 每周 |
| NYT Books API `lists/overview.json` | 英文畅销榜 | 每周三 19:00 美东 |

抓取顺序：豆瓣网页 / 官方接口 → RSSHub → 上一次快照。任何一层失败都不会中断生成。榜单接口里，新书速递返回 `items`，其他榜单返回 `subject_collection_items`，脚本两者都读。

## 改配置

- **加榜单**：打开豆瓣 App 里某个榜单，点右上角分享复制链接，取 `subject_collection/` 后面那串 ID，加进 `scripts/sources.json` 的 `douban` 数组，`section` 填 `hot` 或 `new`。
- **换 RSSHub 实例**：改 `scripts/sources.json` 的 `rsshub_fallback`，填自建实例地址更稳。
- **改经典池 / 年度一本**：编辑 `data/classics.json`、`data/annual.json`。每周按 ISO 周数轮换，同一周重复运行结果一致。
- **改发送时间 / 收件人**：改 `weekly.yml` 里的 `cron`（UTC）；收件人只改 Secret `MAIL_TO`，不用动代码。

## 年度榜单发布时间

11 月深圳读书月；12 月豆瓣、中华读书报、新京报入围、Goodreads、NYT；次年 1 月新京报终选、亚洲周刊；4 月文津图书奖、中国好书。周报会按当前月份列出「本月发布」。
