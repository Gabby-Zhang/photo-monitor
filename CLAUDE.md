# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

一组围绕 Stéphane Séjourné (SS) 和 Gabriel Attal (GA) 的自动监控脚本:抓新闻、盯新照片、同步日程,通过 Telegram / 邮件 / ntfy 推送。是 sa-archive 档案馆(`~/Documents/GitHub/sa-archive`)的上游数据来源之一。

## 各脚本功能

| 脚本 | 功能 | 通知渠道 |
|---|---|---|
| `news_monitor.py` | 按关键词搜两人新闻,用 Claude Haiku(API key,按量计费,不占 Pro 套餐)过滤摘要 | Telegram + Gmail |
| `sejourn_photo_monitor.py` | 监控 EU Audiovisual Service 的 SS REPORTAGE / PHOTO / VIDEO（每类各取最新 100 条，客户端过滤），推 Markdown 日志到 GitHub | ntfy `ss-calendar-update` |
| `photo_library_monitor.py` | 监控图库新照片：Imago、Alamy（JSON API）、Flickr RenewEurope（RSS）、Getty（Playwright+stealth）、EU AV（两轮扫描，见下文）、EP Multimedia（Playwright） | ntfy `photo-alert-gabriel` / `photo-alert-stephane` |
| `sejourn_calendar_sync.py` | 抓 SS 的欧盟委员会官方日程,写入 Apple 日历,推送 ICS 到 GitHub | ntfy |
| `welcome_email.py` | 给 subscribers.json 里的订阅者发欢迎邮件 | Gmail |
| `french_polls/` | 2027 大选民调:scrapers/(elabe、ifop、opinionway、wikipedia)→ polls.db → visualizer.py 出趋势图 | — |

## 云端日历同步架构(重要)

日历核心逻辑在**独立仓库** `Gabby-Zhang/sejourn-calendar`(不在本目录),包含:
- `sync.py` — 云端抓取脚本,每 15 分钟由 cron-job.org 触发 GitHub Actions `workflow_dispatch`
- `sejourn.ics` — 手机 webcal 订阅源
- `sync_state.json` — 已推送事件去重记录，**只由 GitHub Actions 写入**

本地 `sejourn_calendar_sync.py` 仅手动运行(cron 已移除),负责写入 Apple 日历并推送 ICS 文件,**不推送 sync_state.json**（否则会与云端产生竞态条件导致 JSON 损坏）。

手机 webcal 订阅链接：`webcal://raw.githubusercontent.com/Gabby-Zhang/sejourn-calendar/main/sejourn.ics`

## 定时运行

- **GitHub Actions `photo_monitor.yml`** → `photo_library_monitor.py`，每 3 小时一次 + 周二/三 9am–4pm 布鲁塞尔时间每小时一次；支持 `init_mode` 手动触发
- **GitHub Actions `welcome_email.yml`** → `welcome_email.py`，`subscribers.json` 有 push 时自动触发
- **Mac launchd `com.sejourn.photo-monitor.plist`** → `sejourn_photo_monitor.py`，**工作日** 9:00–18:00 每整点
- **Mac launchd `com.photomonitor.attal-sejourne.plist`** → `photo_library_monitor.py`，每 3 小时

改了 plist 要 `launchctl unload ~/Library/LaunchAgents/<name>.plist && launchctl load ...`；改脚本不需要重载。

## 运行命令

```bash
# 日历同步（Mac，需先打开 Calendar.app）
open -a Calendar && sleep 3
python3 sejourn_calendar_sync.py            # 仅未来事件
python3 sejourn_calendar_sync.py --all      # 含历史
python3 sejourn_calendar_sync.py --dry-run  # 预览不写入
python3 sejourn_calendar_sync.py --reset    # 清状态重同步

# 照片监控
python3 sejourn_photo_monitor.py --dry-run
python3 photo_library_monitor.py --init     # 首次运行：仅建缓存不发通知

# 民调
cd french_polls && python3 main.py
```

## 依赖安装

```bash
pip3 install requests beautifulsoup4                       # 日历/新闻基础
pip3 install playwright playwright-stealth feedparser supabase  # 图库监控
python3 -m playwright install chromium
pip3 install -r french_polls/requirements.txt              # 民调
```

## 状态与日志文件（不要手动删，删了会重复通知）

- `sync_state.json` — 日历同步去重
- `photo_sync_state.json` — EU AV 媒体监控去重（`sejourn_photo_monitor.py` 专用）
- `photo_library_seen.json` — 图库照片 ID 去重；GitHub Actions 用 cache 持久化（key `seen-items-{run_id}`，restore 前缀 `seen-items-`），不提交到仓库
- `*.log` — 运行日志，排查问题先看这里

排查云端漏通知：`gh run list --workflow photo_monitor.yml` 找到 run id；`gh run view --log` 经常返回空，改用 `gh api /repos/Gabby-Zhang/photo-monitor/actions/runs/<id>/logs > logs.zip` 再 unzip 看 `0_monitor.txt`。本目录 git remote 是 `Gabby-Zhang/photo-monitor`。

## 环境变量

ANTHROPIC_API_KEY、TELEGRAM_BOT_TOKEN、TELEGRAM_CHAT_ID、GMAIL_USER、GMAIL_APP_PASSWORD、GETTY_API_KEY（可选）、SUPABASE_URL / SUPABASE_SERVICE_KEY（可选）、SEND_TO_ALL（welcome_email 用）

GitHub Actions secrets 与上述同名。

## EU AV Portal API（关键细节）

内部 Solr API：`https://gfdwwnbuul.execute-api.eu-west-1.amazonaws.com/avsportal/avsportal`

- **不支持关键词过滤**：`keyword=`、`q=` 等参数无效，始终返回全量最新记录，关键词匹配必须在客户端做
- 照片缩略图：CDN 前缀 `https://ec.europa.eu/avservices/avs/files/video6/repository/prod/photo/store` + `media_json[分辨率].PATH`（相对路径）
- 视频缩略图：`media_json["16:9"][lang]["THUMB"]` 是完整 URL，无需拼接前缀

**`photo_library_monitor.py` 的两轮扫描**：
- Pass 1：检查 `titles_json` / `summary_json` / `pers_json`（SS 人物 ID = 241448）
- Pass 2：对 Pass 1 未命中的 REPORTAGE，并行逐张抓取每张照片的 caption 检索人名——用于捕捉集体活动（如欧委会全体会议）标题不含人名、但某张照片描述里有的情况

EU AV 自动下载仅本机运行（检测 `GITHUB_ACTIONS` env 跳过），下载到 `~/Pictures/Séjourné_EU_AV/{ref}_{date}_{title}/`，取 `media_json["ORIGINAL"].PATH` 最高规格，下载前校验 `content-type` 以 `image` 开头（CDN 可能返回 200 + HTML 假图）。

## 关键约定与已知坑

- **通知顺序**：先发通知成功，再把 id 加进 seen 记录（顺序反了会漏通知，见 git log）。被 `is_relevant` 过滤掉的 item **不要**加进 seen——下次运行重新判断，否则修了过滤逻辑也永久补不了通知
- **EU AV 结果不走 `process()`**：`run_checks` 里 EU AV / EP Multimedia 有各自独立的处理循环，改 `process()` 不影响它们（曾因此把下载触发写成死代码）
- **HTTP 头只能用 ASCII**：ntfy `Title:` 等头部不能含重音字符（如 `Séjourné`），用 `Sejourn` 代替
- **EU 委员会网站 CDN**：从 GitHub 云端 IP 抓取时可能返回 0 条数据（延迟可达 4+ 小时）；云端 sync.py 用双源策略（网页 + ICS 文件），两处都要加 `"journ" in title.lower()` 过滤，否则会混入其他委员的活动
- **Apple Calendar via AppleScript**：用临时 `.applescript` 文件 + `osascript file.applescript`，**不用** `-e` 参数（多行 script 会报错）；iCloud 日历无法用脚本创建，需用户手动建好
- **Playwright 在 Linux CI**：需要 `Xvfb` 虚拟显示（`DISPLAY=:99`）和 `playwright install-deps chromium`
- **改抓取过滤逻辑**：先用 `--dry-run` 对比改动前后命中差异再上线
