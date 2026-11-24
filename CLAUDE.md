# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

一组围绕 Stéphane Séjourné (SS) 和 Gabriel Attal (GA) 的自动监控脚本:抓新闻、盯新照片、同步日程,通过 Telegram / 邮件 / ntfy 推送。是 sa-archive 档案馆(`~/Documents/GitHub/sa-archive`)的上游数据来源之一。

## 各脚本功能

| 脚本 | 功能 | 通知渠道 |
|---|---|---|
| ~~`news_monitor.py`~~ | **已搬到独立仓库** `~/Documents/GitHub/ssga-news-monitor`(Sonnet,纯邮件,见该仓库 CLAUDE.md);本目录不再有此脚本 | — |
| `sejourn_photo_monitor.py` | 监控 EU Audiovisual Service 的 SS REPORTAGE / PHOTO / VIDEO（每类各取最新 100 条，客户端过滤），推 Markdown 日志到 GitHub | ntfy `ss-calendar-update` + Supabase `photo_alerts`(→ Telegram) |
| `photo_library_monitor.py` | 监控图库新照片：Imago、Alamy（JSON API）、Flickr RenewEurope（RSS）、Getty（Playwright+stealth）、Reuters Connect（需登录，仅本机，见下文）、Shutterstock Editorial（仅本机，见下文）、EU AV（两轮扫描，见下文）、EP Multimedia（Playwright）、理事会 Council Newsroom（JSON API，见下文） | ntfy `photo-alert-gabriel` / `photo-alert-stephane` + Supabase `photo_alerts`(→ Telegram) |
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

> **2026-11-23 起主力触发 = cron-job.org**(同 sejourn-calendar / sa-archive 的做法)。GitHub 自带 `schedule` 实测大量丢跑:11/23 斯特拉斯堡全会日,`photo_monitor.yml` 配了每 20 分钟、全天只跑 4 次;`sejourn_photo_monitor.yml` 配了 ~90 次、只跑 3 次。下面写的 cron 现在只是松散兜底。
> - **`photo_monitor.yml`**:cron-job.org **每 20 分钟**打 `workflow_dispatch`,body `{"ref":"main","inputs":{"tick":"true"}}`。第一步 `scripts/photo_monitor_gate.py` 按**巴黎时间**决定这次跑不跑、跑哪些源:全会日(`PLENARY_WEEKS_2026`,周一–周四)08–20 点每次都跑,但**只有每小时第一个 tick 全扫,其余 tick 用 `--eu-only` 只扫欧盟源**(EU AV / EP Multimedia / Council)——Getty / Imago / Alamy / Flickr 保持每小时,别每 20 分钟撞它们的反爬(用户要求:全会照片本来就来自欧盟源);工作日 09–18 点每小时全扫;其余每 3 小时全扫。**不用再随冬夏令时改 cron**;全会日期**每年在 gate 脚本里更新**。不带 `tick` 的手动触发一律照跑
> - **`sejourn_photo_monitor.yml`**:cron-job.org **每 10 分钟**打 `workflow_dispatch`(`{"ref":"main"}`),无 gate
> - 两个监控的新图都写 Supabase `photo_alerts` → sa-archive `telegram_push.yml` 每 30 分钟推 Telegram。EP Multimedia 以前只发 ntfy、不写表,全会照片从没进过 Telegram(11/23 修);`sejourn_photo_monitor.py` 写表的 id 与 `_eu_av_id(base_ref)` 相同,两边抓到同一组图只推一次,封面换了也不重推

- **GitHub Actions `photo_monitor.yml`** → `photo_library_monitor.py`，工作日 9:00–18:00 巴黎时间每小时一次，其余时段每 3 小时一次；支持 `init_mode` 手动触发。cron 写的是 UTC，按巴黎夏令时(CEST=UTC+2)映射(07:00–16:00 UTC)；冬令时(CET=UTC+1)会整体偏移一小时，需手动调
  - ~~GitHub schedule 里的全会周 `*/20` 条目~~ **2026-11-24 删除**,加频改由 cron-job.org + gate 负责(只扫欧盟源);以下为历史说明
  - **斯特拉斯堡 EP 全会周加频**：全会那 4 天(周一–周四)白天时段提频到**每 20 分钟一次**(07:00–18:00 UTC ≈ 巴黎 09:00–20:00)——大全会日单天 30-50+ 张,60 条上限会漏。用 `day-of-month + month` 精确命中会期(cron 无年份字段,写死的是 **2026** 日期,每年会漂移约一周,需按官方 EP 日历手动更新)。2026 会期:1/19-22、2/9-12、3/9-12、4/27-30、5/18-21、6/15-18、7/6-9、9/14-17、10/5-8、10/19-22、11/23-26、12/14-17(已过的月份 cron 仍留着,明年复用前记得改)。加频后每逢整点 hourly 与 30min 两个 schedule 会撞,靠 workflow 顶部 `concurrency: {group: photo-monitor, cancel-in-progress: false}` 串行化,避免两个 worker 同读 seen.json 各推一遍(cache 按 run_id 存)
- **GitHub Actions `sejourn_photo_monitor.yml`** → `sejourn_photo_monitor.py`(EU AV 监控),纯云端,**不在 Windows/Mac 跑**,详见下一节
- **GitHub Actions `welcome_email.yml`** → `welcome_email.py`，`subscribers.json` 有 push 时自动触发
- ~~Mac launchd `com.sejourn.photo-monitor.plist`~~ / ~~`com.photomonitor.attal-sejourne.plist`~~ — **2026-11 退役**。EU AV 监控(前者)改纯云端;Reuters/Shutterstock(后者)搬到 Windows 常开机器,见下一节。历史坑(plist `&&` 要写 `&amp;&amp;`、`~/Documents` 的 TCC 完全磁盘访问、ntfy `Title` 非 ASCII 的 `UnicodeEncodeError`)仅供查阅旧 Mac 配置时参考,新机器不会再踩

改了 plist 要 `launchctl unload ~/Library/LaunchAgents/<name>.plist && launchctl load ...`；改脚本不需要重载。

## EU AV 监控:云端专属、高频轮询(2026-11 起,取代 Mac launchd)

`sejourn_photo_monitor.py` 纯 HTTP(`requests`),不需要浏览器/登录,**考虑过搬 Windows + 云端兜底,最后决定不搬**——两边跑要同步去重状态,多一层复杂度换不来什么,干脆就留在云端一边跑,把检查频率调高就够用。`.github/workflows/sejourn_photo_monitor.yml`:

- **英国时间 7am-8pm:每 10 分钟一次;其余时间:每小时一次**(`*/10 7-19 * * *` + `0 0-6,20-23 * * *`,UTC)。GitHub Actions 定时任务官方建议的最小间隔是 5 分钟,设更密不保证按时执行、高峰期还可能延迟,**10 分钟是留了余量的稳妥值**,想更密可以改到 5 分钟(文档上限)但不建议再低
- **cron 按 UTC 写,要跟着英国夏令时手动调**:现在(11 月,冬令时 GMT=UTC+0)7am-8pm UK = 07:00-20:00 UTC,就是上面写的时间。英国夏令时(BST=UTC+1,每年 3 月最后一个周日开始、10 月最后一个周日结束)期间要把两段 cron 整体往前挪 1 小时(白天段变 `*/10 6-18 * * *`,夜间段变 `0 19-23,0-5 * * *`)。这和 `photo_monitor.yml` 里巴黎 CEST/CET 的坑是同一类问题,这个仓库已经习惯手动调两次/年
- **去重状态 `photo_sync_state.json` 用 `actions/cache` 存**(和 `photo_monitor.yml` 对 `photo_library_seen.json` 的做法一样),不进 git,干净,不会有"心跳提交刷屏仓库"的问题
- **本仓库是 public**,GitHub Actions 分钟数不计费,提频不产生额外成本
- **已知限制**:`sejourn_photo_monitor.py` 里的 `push_to_github()`(把 Markdown 日志写进 `Gabby-Zhang/sejourn-calendar` 仓库)依赖本机 `~/bin/gh` + 其登录态,在 GitHub Actions 的 ubuntu runner 上找不到会直接跳过("gh CLI not found — skipping GitHub push")——这份 Markdown 日志目前只能靠人在装了 `gh` 的机器上手动跑一次 `sejourn_photo_monitor.py` 来补;**ntfy 推送不受影响**,该收到的通知照常收到

## Windows 常开机器(2026-11 起,取代 Mac launchd 里的本机专属源)

用户的 Mac 不再常开,**Reuters Connect + Shutterstock Editorial 这两个本机专属源**(其余源都在云端跑,见上两节)改到一台可以一直开着的 Windows 电脑,用 Windows 任务计划程序(Task Scheduler)代替 launchd:

- **`photo_library_monitor.py --mac-only`**:**必须**在这台 Windows 机器上跑,架构性原因搬不到云端——Reuters 需要持久登录态的浏览器 profile(`~/.reuters_connect_profile`,`reuters_login.py` 手动登录一次,含设备验证/2FA,只能人工过);Shutterstock 被 Akamai 按数据中心 IP 封(GitHub Actions 直接 403),只有家庭/住宅 IP 能过。**没有、也不可能有云端兜底**——Windows 机器关机期间这两个源就是空窗,和以前 Mac 关机时一样。去重用的 `photo_library_seen.json` **留在本机、不进 git**(和原来 Mac 上一样,与云端的 seen 缓存各自独立,两边都跑会重复推送)。Task Scheduler 直接指向包装脚本(内含 `git pull` 保持代码最新再运行):
  ```
  程序: python.exe
  参数: <repo>\scripts\run_photo_library_monitor_mac_only.py
  触发器: 每 3 小时(对齐原 Mac 频率,可自行调更勤)
  ```
- **Reuters 登录态过期告警**:包装脚本扫监控输出里的 `LOGIN EXPIRED`,命中就推一条 Telegram 提醒(`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`),用本机 `.reuters_login_expired_alerted` 标记文件去重——没修好之前不会每 3 小时重复提醒,登录修好后自动解除

### 密钥:`.env`(仿 Mac launchd 那套,Task Scheduler 不能信任继承环境变量)

`scripts/_env.py` 是个极简 `.env` 加载器,包装脚本启动时会读仓库根目录的 `.env`(不进 git,`.gitignore` 已排除)。原因同 Mac 那边的 launchd `.env`:`Task Scheduler`「不管用户是否登录都运行」模式下不保证能拿到用户会话设的环境变量,所以密钥(`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`,要用 Supabase 的话还有 `SUPABASE_URL`/`SUPABASE_SERVICE_KEY`)直接写本机 `.env` 文件最省心。格式就是 `KEY=value` 一行一个。

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
- `photo_sync_state.json` — EU AV 媒体监控去重（`sejourn_photo_monitor.py` 专用）；GitHub Actions 用 cache 持久化（key `sejourn-av-state-{run_id}`，restore 前缀 `sejourn-av-state-`），不提交到仓库
- `photo_library_seen.json` — 图库照片 ID 去重；GitHub Actions 用 cache 持久化（key `seen-items-{run_id}`，restore 前缀 `seen-items-`），不提交到仓库
- `*.log` — 运行日志，排查问题先看这里

排查云端漏通知：`gh run list --workflow photo_monitor.yml` 找到 run id；`gh run view --log` 经常返回空，改用 `gh api /repos/Gabby-Zhang/photo-monitor/actions/runs/<id>/logs > logs.zip` 再 unzip 看 `0_monitor.txt`。本目录 git remote 是 `Gabby-Zhang/photo-monitor`。

## 环境变量

ANTHROPIC_API_KEY、TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID（Reuters 登录态过期告警用,见「Windows 常开机器」一节;Windows 上走本机 `.env`,不是系统环境变量）、GMAIL_USER、GMAIL_APP_PASSWORD、GETTY_API_KEY（可选）、SUPABASE_URL / SUPABASE_SERVICE_KEY（可选）、SEND_TO_ALL（welcome_email 用）

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

**EU AV 去重 id 只用基础 ref(2026-10 修)**:REPORTAGE 的 `ref` 形如 `P-070821/00-06`,`/00-NN` 是**当前封面照**,欧委会编辑/追加照片时会变 → 旧代码拿整个 `ref` 做 id,封面一换就当新活动再推(27/10 学院例会反复推送即此)。现在 REPORTAGE / PHOTO / Pass 2 三条路径统一用 `_eu_av_id(base_ref)`,同一活动只推一次。换 id 那次首跑靠 `seen["_migrations"]` 标记把当前结果静默入库(API 返回空时不打标记,下次再试)。

**EU AV 追加照片提醒**:欧委会会连续几天往同一组图里加照片,加的可能有他、也可能全是别人。对拍摄日期在 `EU_AV_TRACK_DAYS`(14 天)内的已匹配图片集,每次逐张扫 caption,把「caption 含他名字」的照片序号存到 `seen["_eu_av_photos"][base_ref]`;下次比对,**只有新增的照片里有他**才推「新增 N 张有他(第 x、y 张)」,别人的新图不推。本机运行时顺带下载新增的(已下载的文件会跳过)。第一次扫到某组图只建基线不推;扫描中任一请求失败 → 本次不比对(防止半截扫描下次被当成「新增」);不在结果里的 ref 自动清掉。

**Renew Europe 群体活动（只通知不下载）**：`eu_av_terms` 含 `"renew europe"`，用于捕捉标题/摘要点名 Renew Europe 群组、但不含 Séjourné 本人名字的活动（如领导人会前会、欧理会前碰头，video 示例 `I-290823`）。这类只推通知，**不自动下载**：`run_checks` 里调 `eu_av_download_photos` 前会把 `"renew europe"` 从词表里剔除（`download_terms`），下载函数逐张扫 caption 找他名字，群体照片里没他名字自然下 0 张；他本人的照片照常下载。改 `eu_av_terms` 时注意保持这层「匹配词 ⊇ 下载词」的关系。只靠 `"renew europe"` 命中(标题/摘要没他名字、也没他的 person_id)的条目,标题前加 `RENEW_ONLY_PREFIX`「〔Renew 相关，未见他名字〕」再推(ntfy + `photo_alerts`→Telegram),一眼区分是党团活动(如 2026-11-24 Hayer 的全会周 press briefing `I-298009`)还是他本人的照片;若逐张 caption 扫到他名字则去掉前缀。

## 关键约定与已知坑

- **通知顺序**：先发通知成功，再把 id 加进 seen 记录（顺序反了会漏通知，见 git log）。被 `is_relevant` 过滤掉的 item **不要**加进 seen——下次运行重新判断，否则修了过滤逻辑也永久补不了通知
- **EU AV 结果不走 `process()`**：`run_checks` 里 EU AV / EP Multimedia 有各自独立的处理循环，改 `process()` 不影响它们（曾因此把下载触发写成死代码）
- **标题截断导致 `is_relevant` 误删（重要）**：图说常以地点/日期/职务头衔开头，人名排在很后面。若拿被截断的标题去做 `is_relevant("attal"/"séjourné")` 匹配，会把真照片当无关丢掉。两种成因、两种修法：
  - **Getty**：网站把缩略图 `alt` 截断在 ~147 字符，名字被切掉、且我们拿不到完整文字 → 信任搜索词（query 就是人名全称、服务端已过滤），给结果加 `confirmed_match: True` 跳过 `is_relevant`
  - **Alamy**：完整图说在 API 里能拿到，是我们自己的 `cap[:120]` 把名字切了（Séjourné 曾 30 张漏 29 张）→ **不要截断**，存完整 caption 让 `is_relevant` 看到全名（比 confirmed_match 更精确，仍能挡掉真无关的）
  - 新增源时先确认标题字段是否被截断、人名位置，避免重蹈
- **Imago 绝不拿搜索词当标题(2026-10 修)**:原 `title = alt or query`——缩略图未渲染、alt 为空时标题变成 "Stephane Sejourne",`is_relevant` 必然放行;而不带重音的搜索在 Imago 是模糊匹配、会搜出别的 Stéphane → 推送「3 张新图」点进去(带重音搜索页)却什么都没有。现在 alt 为空就留空,被过滤、不进 seen,下次重判。同时去掉了 scraper 里大小写/重音敏感的 query 关键词预过滤,统一交给 `is_relevant`(同时认 `séjourné` / `sejourne`)。`process()` 现会把每条推送的标题+链接记进日志(`+ …`),查可疑提醒先看这个
- **单次抓取上限**：Getty / EP Multimedia 取前 60 条（原为 20）。大活动日（贸易展、全会）单天 30-50+ 张，3 小时跑一次时排在前 20 之后的会被挤掉漏掉。Getty 搜索页一次渲染约 60 张缩略图，故 60 有效；再多需翻页
- **Reuters Connect 必须登录（仅 Mac 本机跑）**：未登录访问搜索会被转到 `/login`，登录又有设备验证、只能人手过。所以 `photo_library_monitor.py` 复用一个持久浏览器资料夹 `~/.reuters_connect_profile`（优先用本机真 Google Chrome，去掉自动化标记），由 **`python3 reuters_login.py`** 手动登录一次存下会话。搜索地址是登录后的 `/app/feed/pictures?query=<人名>`（旧的 `/search?searchTerm=` 未登录可用、登录后会被带回首页），图片链接格式 `/app/item/...`。卡片标题是通讯社 slug(如 `FRANCE-NOUMEA-POLITICAL-MEETING`)、多数不含人名,所以同 Getty 一样**信任搜索、加 `confirmed_match`**。排序「Newest First」存在账号偏好里、不在 URL 上。GitHub Actions 上没有这个资料夹 → 自动跳过此源。日志出现 `LOGIN EXPIRED` 就重跑 `reuters_login.py`；跑 `reuters_login.py` 时别同时跑监控（同一资料夹会被锁）。**2026-11 起这个不用自己盯日志了**:`scripts/run_photo_library_monitor_mac_only.py` 会扫监控输出里的 `LOGIN EXPIRED`,命中就推一条 Telegram 提醒(`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`),用本机 `.reuters_login_expired_alerted` 标记文件去重——没修好之前不会每 3 小时重复提醒,登录修好后自动解除,下次再过期还会再提醒
- **Shutterstock 只搜 Editorial 库、仅本机跑**（`/fr/editorial/search/<slug>?sort=date&person=<id>`,人物 id:Attal `85506`、Séjourné `110070`,URL 写在 `PERSONS[...]['search_urls']`,每人只抓一次;**排序参数是 `sort=date`,写成 `sort=newest` 直接 400**;新闻图不在主图库搜索里;详情页链接形如 `/fr/editorial/image-editorial/<slug>-17210815ad`,末段为图片 id）。Akamai 封数据中心 IP（云端沙箱/GitHub Actions 直接 403），所以检测到 `GITHUB_ACTIONS` 就跳过，只在 Mac launchd `--mac-only` 那一路跑。alt/URL slug 都是截断图说 → 同 Getty 用 `confirmed_match`。被拦时记 warning 返回空、不报错。2026-09 写好但没合并过,2026-10 移植进来
- **EP Multimedia 用 photoset 级别、不用单张 photo（重要，2026-07 修）**：search JSON 里 `results.photos.content` 是单张照片，其 `mediaBusinessId`（如 `20260707_EP-208082A_VZ1_EG_0083`）**不是**合法的 `/photoset/` 地址（点开 404），且一场活动含几十张 → 旧代码既发死链又对同一活动刷几十条通知。改读 `results.photosets.content`（相册级，`mediaBusinessId` 形如 `EP-208082A`），URL 拼 `/en/photoset/{标题slug}_{相册id}`。slug = 小写、去重音、去撇号（`EU's`→`eus`）、其余非字母数字压成单 `-`（见 `_ep_slug`，已对 18 条实测全中）。**search 只按 `orderBy=newest` 返回最新 ~18 个相册、不分年份（能回溯到 14 个月前）**，故用 `EP_NOTIFY_DAYS`（14 天）过滤:超窗的老相册**静默塞进 seen 不推**（同日历「旧 backlog 静默入库」），只对窗内新相册推。去重 id 从单张换到相册级——若不加日期窗,换 id 那次首跑会把全部 18 个（含 2025 的）当新的全推一遍
- **Council Newsroom（欧盟理事会图库，2026-11 加）**：前端 `newsroom.consilium.europa.eu` 是 Angular SPA，数据走公开 JSON 接口 `https://newsroomapi.consilium.europa.eu/api/photos?pagesize=100&page=1&keywords=<词>&orderBy=date&orderDirection=descending`，requests 直接拿、不用浏览器。**关键词不认重音**：`Séjourné` 返回 0 条、`Sejourne` 才行 → 每人一个专用 `consilium_query`，每人只查一次。接口返回单张照片，一场会（Roundtable / Bilateral / Press conference）是一个 gallery、几十张 → 同 EP 按 `galleryId` 去重，一个 gallery 推一次；每张照片的 caption+tags 再过一遍 `is_relevant`（caption 人名全大写如 `Stéphane SEJOURNE`，小写后命中 `sejourne`）。链接 = `/events/{linkedEvent.urlName}/{galleryUrlSegment}/{uniqueId}`（前端 bundle 里就这么拼，实测可打开）。搜索会返回多年历史 → 超过 `EP_NOTIFY_DAYS`（14 天）的 gallery 静默入库，首跑不刷屏。同一 gallery 后续追加照片不会再推。
- **HTTP 头只能用 ASCII**：ntfy `Title:` 等头部不能含重音字符（如 `Séjourné`），用 `Sejourn` 代替
- **聚合日历页改版 = 委员 facet 已废（2026-06 起，重要）**：欧委会把学院日历页迁到 OpenEuropa List Pages，旧的 `?f[0]=commissioner_dynamic_commissioner_dynamic:…COM_…` 过滤参数被**完全忽略**（连官网自己的 “See all” 都失效），分页 `?page=` 也不推进、`Past` 状态筛选不生效——**整套 facet/分页当前对所有人都坏**。更坑的是 **Séjourné 直接从委员下拉里消失**（以前也出现过），所以**任何挂在「他的 filter / facet 选项」上的方案都是脆的**。对策：抓全体 + `"journ" in title` 客户端筛（filter-independent），并以 Transparency Register 作实时兜底。欧委会修好分页后聚合页源会自动恢复完整覆盖，无需改码。
- **EU 委员会网站 CDN**：从 GitHub 云端 IP 抓取仍可能返回 0 条 / 延迟数小时；客户端名字筛选（`"journ" in title.lower()`）务必保留，否则会混入其他委员的活动
- **Apple Calendar via AppleScript**：用临时 `.applescript` 文件 + `osascript file.applescript`，**不用** `-e` 参数（多行 script 会报错）；iCloud 日历无法用脚本创建，需用户手动建好
- **Playwright 在 Linux CI**：需要 `Xvfb` 虚拟显示（`DISPLAY=:99`）和 `playwright install-deps chromium`
- **改抓取过滤逻辑**：先用 `--dry-run` 对比改动前后命中差异再上线
