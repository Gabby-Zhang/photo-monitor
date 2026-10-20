# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

一组围绕 Stéphane Séjourné (SS) 和 Gabriel Attal (GA) 的自动监控脚本:抓新闻、盯新照片、同步日程,通过 Telegram / 邮件 / ntfy 推送。是 sa-archive 档案馆(`~/Documents/GitHub/sa-archive`)的上游数据来源之一。

## 各脚本功能

| 脚本 | 功能 | 通知渠道 |
|---|---|---|
| ~~`news_monitor.py`~~ | **已搬到独立仓库** `~/Documents/GitHub/ssga-news-monitor`(Sonnet,纯邮件,见该仓库 CLAUDE.md);本目录不再有此脚本 | — |
| `sejourn_photo_monitor.py` | 监控 EU Audiovisual Service 的 SS REPORTAGE / PHOTO / VIDEO（每类各取最新 100 条，客户端过滤），推 Markdown 日志到 GitHub | ntfy `ss-calendar-update` |
| `photo_library_monitor.py` | 监控图库新照片：Imago、Alamy（JSON API）、Flickr RenewEurope（RSS）、Getty（Playwright+stealth）、Reuters Connect（需登录，仅本机，见下文）、EU AV（两轮扫描，见下文）、EP Multimedia（Playwright） | ntfy `photo-alert-gabriel` / `photo-alert-stephane` |
| `sejourn_calendar_sync.py` | 抓 SS 的欧盟委员会官方日程,写入 Apple 日历,推送 ICS 到 GitHub | ntfy |
| `welcome_email.py` | 给 subscribers.json 里的订阅者发欢迎邮件 | Gmail |
| `french_polls/` | 2027 大选民调:scrapers/(elabe、ifop、opinionway、wikipedia)→ polls.db → visualizer.py 出趋势图 | — |

## 云端日历同步架构(重要)

日历核心逻辑在**独立仓库** `Gabby-Zhang/sejourn-calendar`(不在本目录),包含:
- `sync.py` — 云端抓取脚本,每 15 分钟由 cron-job.org 触发 GitHub Actions `workflow_dispatch`
- `sejourn.ics` — 手机 webcal 订阅源
- `sync_state.json` — 已推送事件去重记录，**只由 GitHub Actions 写入**

本地 `sejourn_calendar_sync.py` 仅手动运行(cron 已移除),负责写入 Apple 日历并推送 ICS 文件,**不推送 sync_state.json**（否则会与云端产生竞态条件导致 JSON 损坏）。

**数据源（2026-06 改版后，三源合并去重 by `date|title`）**：
1. **聚合学院日历页** — 不带任何委员过滤地抓全体，再用 `"journ" in title` 客户端筛。**绝不依赖他的 facet 选项**（见下方坑）。检测到 `?page=` 不推进就停。
2. **Transparency Register 会议**（`meeting.do?host=<uuid>`）— 他本人 host `d8fba42d-…` + Cabinet host `21deeb50-…`。纯表格、实时，是聚合页分页坏掉期间唯一活的源；只覆盖「与利益相关方的会议」，不含理事会/演讲等。
3. **仓库内 `sejourn.ics`** — 跨次运行保留历史。

**跨源去重**:同一场会议会以两种标题出现——Transparency 版 `Séjourné meets <org>` 与官方议程版 `Executive Vice-President Séjourné meets Mr X … <org>`,`date|title` 抓不到。`drop_transparency_duplicates` 按「同日 + 机构名命中」把 Transparency 风格那条丢掉、保留更详细的议程版;**不分来源**(连已焙进旧 ICS 的重复也清)。改前先确认机构名匹配:先整段 `<org>`(去括号)子串匹配,再退化到 ≥5 字的去停用词 token(停用词表含 france/paris/european 等地理与泛称,防误杀)。档案馆「📆 行程日历」页实时解析这个 ICS、不入库,所以**改 ICS 即改档案馆**,无需动 Supabase。

ICS 的 UID 用 `md5(date|title)` **确定性**生成（不能用 `hash()`，Python 字符串 hash 每进程加盐会让 UID 每次变 → 订阅端重复提醒）。通知按来源打标（🤝 会议 / 🏛 日程），且只对滚动窗口（`DAYS_BACK`）内的新事件推送，旧 backlog 静默入库。

手机 webcal 订阅链接：`webcal://raw.githubusercontent.com/Gabby-Zhang/sejourn-calendar/main/sejourn.ics`

## 定时运行

- **GitHub Actions `photo_monitor.yml`** → `photo_library_monitor.py`，工作日 9:00–18:00 巴黎时间每小时一次，其余时段每 3 小时一次；支持 `init_mode` 手动触发。cron 写的是 UTC，按巴黎夏令时(CEST=UTC+2)映射(07:00–16:00 UTC)；冬令时(CET=UTC+1)会整体偏移一小时，需手动调
  - **斯特拉斯堡 EP 全会周加频**：全会那 4 天(周一–周四)白天时段提频到**每 20 分钟一次**(07:00–18:00 UTC ≈ 巴黎 09:00–20:00)——大全会日单天 30-50+ 张,60 条上限会漏。用 `day-of-month + month` 精确命中会期(cron 无年份字段,写死的是 **2026** 日期,每年会漂移约一周,需按官方 EP 日历手动更新)。2026 会期:1/19-22、2/9-12、3/9-12、4/27-30、5/18-21、6/15-18、7/6-9、9/14-17、10/5-8、10/19-22、11/23-26、12/14-17(已过的月份 cron 仍留着,明年复用前记得改)。加频后每逢整点 hourly 与 30min 两个 schedule 会撞,靠 workflow 顶部 `concurrency: {group: photo-monitor, cancel-in-progress: false}` 串行化,避免两个 worker 同读 seen.json 各推一遍(cache 按 run_id 存)
- **GitHub Actions `welcome_email.yml`** → `welcome_email.py`，`subscribers.json` 有 push 时自动触发
- **Mac launchd `com.sejourn.photo-monitor.plist`** → `sejourn_photo_monitor.py`，**工作日** 9:00–18:00 每整点。plist 用 `/bin/bash -c 'dow=$(date +%u); [ "$dow" -le 5 ] &amp;&amp; /usr/bin/python3 …'` 限工作日——**plist 里 `&&` 必须写成 `&amp;&amp;`**,裸 `&&` launchd 能容忍但 `plutil`/`PlistBuddy` 拒读、改不了。2026-10 修:原用 `/bin/sh`(无完全磁盘访问 → `Operation not permitted`,退出码 2)、日志在 `~/Documents`,改为 `/bin/bash` + 日志 `~/Library/Logs/sejourn-photo-monitor.log`;另修 ntfy `Title` 含 `—` 导致 `UnicodeEncodeError`(见下方「HTTP 头只能用 ASCII」)。注意 `sejourn_photo_monitor.py` 先写 state 再发通知,通知失败会丢那批
- **Mac launchd `com.photomonitor.attal-sejourne.plist`** → `photo_library_monitor.py --reuters-only`，每 3 小时。**只跑 Reuters**(唯一需要本机登录态的源);其余源由 GitHub Actions 负责——本机和云端 seen 文件各自独立,两边都跑会让每张新图推两次。plist 现为 `/bin/bash -c "<CLT python3> …/photo_library_monitor.py --reuters-only"`。**`~/Documents` 受 TCC 保护的两个坑(2026-10 修,此前自 7 月起本机任务一直没跑成、seen 文件停在 7/7):① plist 的 `StandardOutPath`/`StandardErrorPath` 由 launchd 自己打开,指向 `~/Documents` 就直接 `78: EX_CONFIG`,脚本一行都不跑——已挪到 `~/Library/Logs/photo-monitor-launchd.log`;② 脚本本身读写 `~/Documents` 靠给 `/bin/bash` 开「完全磁盘访问」。** 验证:`launchctl kickstart -k gui/$(id -u)/com.photomonitor.attal-sejourne`,`launchctl list | grep photomonitor` 退出码应为 0

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

**Renew Europe 群体活动（只通知不下载）**：`eu_av_terms` 含 `"renew europe"`，用于捕捉标题/摘要点名 Renew Europe 群组、但不含 Séjourné 本人名字的活动（如领导人会前会、欧理会前碰头，video 示例 `I-290823`）。这类只推通知，**不自动下载**：`run_checks` 里调 `eu_av_download_photos` 前会把 `"renew europe"` 从词表里剔除（`download_terms`），下载函数逐张扫 caption 找他名字，群体照片里没他名字自然下 0 张；他本人的照片照常下载。改 `eu_av_terms` 时注意保持这层「匹配词 ⊇ 下载词」的关系。

## 关键约定与已知坑

- **通知顺序**：先发通知成功，再把 id 加进 seen 记录（顺序反了会漏通知，见 git log）。被 `is_relevant` 过滤掉的 item **不要**加进 seen——下次运行重新判断，否则修了过滤逻辑也永久补不了通知
- **EU AV 结果不走 `process()`**：`run_checks` 里 EU AV / EP Multimedia 有各自独立的处理循环，改 `process()` 不影响它们（曾因此把下载触发写成死代码）
- **标题截断导致 `is_relevant` 误删（重要）**：图说常以地点/日期/职务头衔开头，人名排在很后面。若拿被截断的标题去做 `is_relevant("attal"/"séjourné")` 匹配，会把真照片当无关丢掉。两种成因、两种修法：
  - **Getty**：网站把缩略图 `alt` 截断在 ~147 字符，名字被切掉、且我们拿不到完整文字 → 信任搜索词（query 就是人名全称、服务端已过滤），给结果加 `confirmed_match: True` 跳过 `is_relevant`
  - **Alamy**：完整图说在 API 里能拿到，是我们自己的 `cap[:120]` 把名字切了（Séjourné 曾 30 张漏 29 张）→ **不要截断**，存完整 caption 让 `is_relevant` 看到全名（比 confirmed_match 更精确，仍能挡掉真无关的）
  - 新增源时先确认标题字段是否被截断、人名位置，避免重蹈
- **单次抓取上限**：Getty / EP Multimedia 取前 60 条（原为 20）。大活动日（贸易展、全会）单天 30-50+ 张，3 小时跑一次时排在前 20 之后的会被挤掉漏掉。Getty 搜索页一次渲染约 60 张缩略图，故 60 有效；再多需翻页
- **Reuters Connect 必须登录（仅 Mac 本机跑）**：未登录访问搜索会被转到 `/login`，登录又有设备验证、只能人手过。所以 `photo_library_monitor.py` 复用一个持久浏览器资料夹 `~/.reuters_connect_profile`（优先用本机真 Google Chrome，去掉自动化标记），由 **`python3 reuters_login.py`** 手动登录一次存下会话。搜索地址是登录后的 `/app/feed/pictures?query=<人名>`（旧的 `/search?searchTerm=` 未登录可用、登录后会被带回首页），图片链接格式 `/app/item/...`。卡片标题是通讯社 slug(如 `FRANCE-NOUMEA-POLITICAL-MEETING`)、多数不含人名,所以同 Getty 一样**信任搜索、加 `confirmed_match`**。排序「Newest First」存在账号偏好里、不在 URL 上。GitHub Actions 上没有这个资料夹 → 自动跳过此源。日志出现 `LOGIN EXPIRED` 就重跑 `reuters_login.py`；跑 `reuters_login.py` 时别同时跑监控（同一资料夹会被锁）
- **EP Multimedia 用 photoset 级别、不用单张 photo（重要，2026-07 修）**：search JSON 里 `results.photos.content` 是单张照片，其 `mediaBusinessId`（如 `20260707_EP-208082A_VZ1_EG_0083`）**不是**合法的 `/photoset/` 地址（点开 404），且一场活动含几十张 → 旧代码既发死链又对同一活动刷几十条通知。改读 `results.photosets.content`（相册级，`mediaBusinessId` 形如 `EP-208082A`），URL 拼 `/en/photoset/{标题slug}_{相册id}`。slug = 小写、去重音、去撇号（`EU's`→`eus`）、其余非字母数字压成单 `-`（见 `_ep_slug`，已对 18 条实测全中）。**search 只按 `orderBy=newest` 返回最新 ~18 个相册、不分年份（能回溯到 14 个月前）**，故用 `EP_NOTIFY_DAYS`（14 天）过滤:超窗的老相册**静默塞进 seen 不推**（同日历「旧 backlog 静默入库」），只对窗内新相册推。去重 id 从单张换到相册级——若不加日期窗,换 id 那次首跑会把全部 18 个（含 2025 的）当新的全推一遍
- **HTTP 头只能用 ASCII**：ntfy `Title:` 等头部不能含重音字符（如 `Séjourné`），用 `Sejourn` 代替
- **聚合日历页改版 = 委员 facet 已废（2026-06 起，重要）**：欧委会把学院日历页迁到 OpenEuropa List Pages，旧的 `?f[0]=commissioner_dynamic_commissioner_dynamic:…COM_…` 过滤参数被**完全忽略**（连官网自己的 “See all” 都失效），分页 `?page=` 也不推进、`Past` 状态筛选不生效——**整套 facet/分页当前对所有人都坏**。更坑的是 **Séjourné 直接从委员下拉里消失**（以前也出现过），所以**任何挂在「他的 filter / facet 选项」上的方案都是脆的**。对策：抓全体 + `"journ" in title` 客户端筛（filter-independent），并以 Transparency Register 作实时兜底。欧委会修好分页后聚合页源会自动恢复完整覆盖，无需改码。
- **EU 委员会网站 CDN**：从 GitHub 云端 IP 抓取仍可能返回 0 条 / 延迟数小时；客户端名字筛选（`"journ" in title.lower()`）务必保留，否则会混入其他委员的活动
- **Apple Calendar via AppleScript**：用临时 `.applescript` 文件 + `osascript file.applescript`，**不用** `-e` 参数（多行 script 会报错）；iCloud 日历无法用脚本创建，需用户手动建好
- **Playwright 在 Linux CI**：需要 `Xvfb` 虚拟显示（`DISPLAY=:99`）和 `playwright install-deps chromium`
- **改抓取过滤逻辑**：先用 `--dry-run` 对比改动前后命中差异再上线
