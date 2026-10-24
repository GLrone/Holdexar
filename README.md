<div align="center">

<br>

# Holdexar

**本地 Steam 多区价格监控终端**

[![Version](https://img.shields.io/badge/version-v0.3.5--beta-orange)](../../releases)
[![Python 3.13+](https://img.shields.io/badge/Python-3.13%2B-blue?logo=python&logoColor=white)](https://www.python.org)
[![FastAPI](https://img.shields.io/badge/Backend-FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Vue 3](https://img.shields.io/badge/Frontend-Vue%203-42b883?logo=vuedotjs&logoColor=white)](https://vuejs.org)
[![Platform](https://img.shields.io/badge/Platform-Windows%2010%2F11-0078D4?logo=windows&logoColor=white)](https://github.com)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

</div>

---

# 使用手册（写给玩家）

## 这是什么？

你在 Steam 上买游戏时可能遇到过：同一个游戏，阿根廷区、土耳其区、国区的价格差好几倍。**Holdexar 就是帮你把这件事自动盯起来的工具**——

- 你只需要告诉它「我想盯哪些游戏」：粘贴游戏链接、登录 Steam 同步愿望单、或者导入一份列表；
- 剩下的全由它自己来：每隔几小时把各区价格抓回来、按实时汇率折算成人民币、发现降价和历史新低；
- 价格降到你的心理价位时，它自动发邮件提醒你。

它是一个**绿色软件**：解压就能用，不需要安装、不需要注册账号、不需要懂任何技术。所有数据都存在你自己的电脑里，不连接任何云端服务器，也没有任何数据上报。

## 三步上手

### 第 1 步：下载并打开

1. 到 [Releases](../../releases) 页面下载 `Holdexar-win64-v<版本>.zip`；
2. 解压到任意文件夹，双击 `Holdexar/Holdexar.exe`；
3. 第一次打开会弹出安全提示（未签名程序的正常现象，不是病毒），点「更多信息 → 仍要运行」即可，详见下方[安全提示](#首次运行安全提示)。

打开后会有一遍新手导览，跟着「下一步」走就行，随时可以跳过。

### 第 2 步：告诉它你想盯哪些游戏

三种方式任选，可以混着用：

- **粘贴链接**：在【仪表盘】的添加框里粘贴任意 Steam 商店链接（比如 `https://store.steampowered.com/app/105600/`），回车即可；
- **登录 Steam 同步**：到【设置】绑定 Steam 账号（支持账号密码直接登录，遇到手机验证码按提示输入即可），愿望单和已购游戏会自动进来；
- **从游戏卡片关注**：逛【游戏商店】时，看到想长期盯价的游戏，点卡片右上角的**星标**即可。

### 第 3 步：设一条降价提醒

到【价格提醒】页，建一条规则，比如「泰拉瑞亚 国区 ≤ ¥42 时通知我」。之后每当价格刷新，符合条件就会自动发邮件到你预留的邮箱。

到这里核心用法就完成了。**接下来你什么都不用做**——程序会自己定时更新价格、自己发现降价、自己发提醒。你随时打开看最新数据就行。

## 它能帮我做什么？

| 功能 | 说明（人话版） |
|---|---|
| 游戏商店 | 41 个国家/地区的价格一屏对比：当地标价、折扣、折算人民币、史低标记；可按折扣和锁区筛选 |
| 游戏详情 | 同一游戏的标准版/豪华版等多版本比价；每个区的历史价格走势曲线 |
| 价格提醒 | 降价、历史新低自动发邮件；阈值条件自己定 |
| 愿望单与已购 | 绑定 Steam 后自动同步，不用手动维护 |
| 家庭共享 | 家庭组成员的游戏库聚合分析：谁贡献了什么、值不值 |
| 账单 | Steam 消费记录同步，多币种按当日汇率折算成人民币 |
| 捆绑包 / 免费游戏 | Humble Choice 月包、Epic 喜加一、Steam 免费游戏动态，换新自动提示 |
| 活动日历 | Steam 官方大促（夏促/冬促）和新品游戏节的时间表与倒计时 |
| 汇率 | 39 种货币兑人民币走势，自带 2010 年至今的历史档案；卡商充值价目参考 |
| 工具箱 | 礼品卡 CDK 批量激活，逐码出回执 |

## 灵动岛：变化会自己来找你

窗口顶部有一颗「灵动岛」胶囊，程序里**值得你知道的事**都会在那里冒出来，不需要你主动翻页面：

- 价格更新到哪了（正在更新 / 已更新 / 部分暂缺）；
- Humble Choice 月包换新了、Epic 喜加一轮换了；
- 有新版本可以更新了。

点开胶囊能看详情和进度，不需要知道任何内部机制。

## 首次运行安全提示

> [!IMPORTANT]
> 本应用发布包**未经商业代码签名**（个人开源项目），首次在 Windows 运行可能会遇到系统或安全软件的风险拦截提示，特此说明其背景与安全建议：
>
> 1. **核对官方发布哈希（最重要防线）**：请**务必**只从官方 [Releases](../../releases) 页面下载，并在下载后核对官方公布的 SHA256 校验和（PowerShell 命令：`Get-FileHash Holdexar-win64-*.zip -Algorithm SHA256`）。只要哈希一致，即可确认为未经篡改的官方构建版本。
> 2. **优先推荐源码运行**：若你对未签名二进制心存疑虑，或系统安全策略禁止运行未签名程序，**强烈建议直接以源码方式运行**（执行 `git clone` 并运行 `python run.py`）。源码完全公开、依赖透明可审计，无需放行任何未经签名的二进制。
> 3. **Windows SmartScreen 提示**（「Windows 已保护你的电脑」）：属于系统对无签名新程序的通用警示。在确认下载自官方 Releases 且 SHA256 校验匹配后，可点击 **更多信息 → 仍要运行**。
> 4. **Smart App Control 阻断**：Windows 11 的 Smart App Control 可能会直接拦截未经签名的可执行文件。**不建议为了运行单个软件关闭系统级安全防护**；若遇阻断，推荐直接采用源码模式运行（`python run.py`）。
> 5. **安全软件误报说明**：PyInstaller 打包的便携应用与代理网络组件偶见启发式误报。如遇告警，可将压缩包上传至 VirusTotal 等多引擎平台复验；确认官方校验和一致且决定信任后，可仅针对主程序单文件设置排除，不建议盲目关闭全局防护。

## 常见问题（FAQ）

<details>
<summary>首次运行弹出「Windows 已保护你的电脑」？</summary>

Windows 系统对无商业签名程序的标准警示。请先核对下载包的 SHA256 与官方 Release 公布值是否一致；确认无误后点击 **更多信息 → 仍要运行** 即可，同一版本后续不会重复提示。
</details>

<details>
<summary>弹出「此应用已被阻止」（Smart App Control）？</summary>

这是 Windows 11 Smart App Control 对未签名程序的系统级拦截策略。我们**不建议为了单个软件关闭系统的安全防护功能**。遇到此类阻断时，建议通过源码运行（克隆仓库后执行 `python run.py`），源码环境完全可审计，无需依赖打包二进制。
</details>

<details>
<summary>安全软件提示风险或报毒？</summary>

无签名便携程序及网络代理组件常被部分杀毒软件的启发式规则误标记。建议将安装包上传至 VirusTotal 等平台进行多引擎核验；如官方 SHA256 一致且确认信任，可仅对本程序执行文件添加信任规则，切勿盲目关闭防病毒软件。
</details>

<details>
<summary>登录 Steam 时收不到验证码 / 登录不上？</summary>

应用内登录走 Steam 官方认证接口。验证码分两种：邮箱验证码去邮箱收；手机令牌打开 Steam 手机 App 就能看到。输错可以重新输入；如果开启了手机 App 确认，直接在手机上点「确认」即可，程序会自动继续。密码只在本机内存里用一次，不会保存。
</details>

<details>
<summary>刚装好，部分游戏没有价格？</summary>

新加的游戏会尽快补齐首趟价格（你星标关注的排最前）；刚装好时的批量数据要逐步积累，等一轮更新周期走完就好。可以随时手动触发一轮。
</details>

<details>
<summary>价格和 Steam 商店页不一致？</summary>

价格每 6 小时自动刷新一轮，两轮之间的临时调价有滞后；每条价格都带抓取时间，卡片上能看到数据的新旧。
</details>

<details>
<summary>我的账号密码安全吗？数据存在哪？连接了哪些网络服务？</summary>

1. **凭据安全**：账号密码登录直调 Steam 官方认证接口，密码仅在登录鉴权时于内存中临时使用，**绝不落盘、不写日志**；鉴权成功后的凭据采用 AES-256-GCM 本地加密保存。
2. **数据存储**：全部数据（SQLite 数据库与日志）保存在你自己的电脑上，新装默认优先保存在程序根目录 `data/` 下（完全便携同盘携带），若程序目录无写权限则自动安全回退至 `%LOCALAPPDATA%\Holdexar`。
3. **网络服务与隐私**：**零云端服务器、零账号系统、零行为遥测与隐私上报**。为了实现比价与资讯功能，程序直接与以下公开服务通信：
   - Steam 商店与用户数据相关网络请求；
   - Frankfurter API（欧洲央行公开基准汇率折算）；
   - Epic Games Store / Humble Bundle / Barter.vg（公开促销与免费游戏资讯）；
   - GitHub Releases 与加速镜像（检查更新与按需下载数据种子）；
   - 用户自备并主动接入的代理订阅源或 SMTP 邮件通知服务。
</details>

<details>
<summary>不想让它自动抓价格？</summary>

【自动抓取】页关闭「自动价格更新」即可：定时刷新和自动重试会停，手动抓取不受影响，随时能再打开。
</details>

<details>
<summary>想要更稳的自动更新和更快的抓取？</summary>

到【网络】页接入你自己的 Clash 订阅（程序不内置任何订阅源）。这是可选项——不接入也能正常加游戏、看已有数据。
</details>

## 更新与升级

- 有新版本时启动会主动提示一次，侧栏「我」项常驻红点，顶栏出现更新胶囊（发现新版 / 下载中 / 待重启）；进【我 → 应用更新】一键升级。
- 换装只替换程序文件，用户数据、账号绑定、价格历史一概不动；换装中断会自动回滚到原版本并重新打开应用。
- 也可以手动下载新版解压覆盖，数据不受影响。

**Scoop 渠道**（便携应用，不写注册表、不装服务）：

```powershell
scoop bucket add holdexar https://github.com/GLrone/scoop-bucket
scoop install holdexar
```

每个版本 Release 附带的核心资产：
- 轻量应用包 `Holdexar-win64-v<版本>.zip`：解压即用，随包附带开源许可原文与来源说明，不捆绑内核二进制与数百兆种子；
- 公共数据种子 `holdexar_seed.db.gz`：独立资产（压缩分发），应用首次启动按需下载，源码用户亦可通过 `run.py` 自动获取；
- 公共目录库模板 `holdexar_template.db`：初始公共游戏目录模板（若随版发布）；
- 更新清单 `latest.json`：挂载于固定 tag `updater` 供客户端检查更新读取。

---

# 开发者文档

## 技术架构

| 层 | 组件 |
|---|---|
| 后端 | FastAPI · SQLAlchemy(async) · APScheduler(25 个定时任务) · aiohttp 爬核 |
| 前端 | Vue 3 · TypeScript · Vite · 自制 Hl\* 组件体系(深浅双主题 / 中英双语) |
| 桌面 | pywebview(WebView2) · onedir 绿色包 · 单实例锁 |
| 网络 | 出网策略引擎(proxy_first 默认 / direct_only / direct_first / proxy_only) · 自备 Clash 订阅 · mihomo 内核按需获取与轻量分发 |
| 认证 | 应用内账号密码登录直调 Steam IAuthenticationService;手动 Cookie 粘贴通道保留;凭据 AES-256-GCM 加密落库 |

### 业务主线

价格系统的组织主线：

```
Monitor Pool → Price Refresh Cycle → Price Observation
→ Coverage / Freshness → Price Event → 用户结果
```

- **Monitor Pool**：持续监控的对象集合，只由用户显式动作建立（关注 / 同步 / 导入）；「添加游戏」仅入 Catalog + 一次性首爬，不等于建立监控。
- **Price Refresh Cycle**：唯一的价格刷新生命周期，每轮由多段任务串成（监控池 → 库内其余 → 目录层随开关 → 特惠榜尾段恒随轮），锚点网格对齐 Steam 折扣刷新时刻。
- **Observation 与 Event 分离**：`game_price_history` 一行是一次观察；事件层（降价 / 史低 / 永降）在观察之上独立判定。

### 自动化设计

- 用户的输入边界是封闭清单（绑定账号 / 关注 / 添加订阅 / 添加游戏），此后的一切自动完成：订阅导入即体检、体检健康自动准入、出口漂移启动热收敛、重建空池自愈、数据整体过期自动提前首轮刷新。
- 停自动化走白名单：仅数据泄露 / 登录信息过期 / 订阅全失效会停下等用户，其余失败一律自动降级、重试、换通道。

## 源码运行

要求 Python ≥3.13、Node ≥ 20.19(或 ≥ 22.12;Vite 8 的 `engines` 要求):

```bash
git clone https://github.com/GLrone/Holdexar.git
cd Holdexar
python run.py            # 自动建 venv → 装依赖 → 构建前端 → 拉起桌面窗口
```

```bash
python run.py --server   # 仅本地服务,浏览器访问 http://127.0.0.1:28765
python run.py --dev      # 对接 Vite 热更新开发
python run.py --port N   # 指定端口
```

首次运行会自动补齐公共数据种子(历史汇率与价格历史切片压缩包 `holdexar_seed.db.gz`):种子是二进制大文件,不进 git,由 `run.py` 从 Release 资产拉取。没网也不影响启动,只是【汇率】页没有历史档案,之后重跑 `python scripts/fetch_seed.py` 即可。

随包内核采用轻量模式分发：内置开源许可原文与来源说明，不捆绑内核二进制；运行所需内核由客户端在首次使用时按需下载或由用户自备。源码用户可通过 `python scripts/fetch_kernel.py` 获取完整内核，或通过 `--license-only` 仅就地准备许可材料。

## 打包与发布

发布机(建议用 `server/.venv` 的解释器):

```bash
python scripts/build_release.py          # npm build → PyInstaller → 消毒 → zip,并生成清单
#   编辑 release/RELEASE_NOTES.md 写本次 changelog(客户端检查更新直接展示这段文字)
python scripts/publish_release.py --dry-run   # 预览发布动作,不碰网络
python scripts/publish_release.py             # 正式发布(需 gh CLI 且已 gh auth login)
```

一次正式发布会产生**两条 Release**:

| Release | tag | 内容 | 作用 |
|---|---|---|---|
| 版本发布 | `v<版本>` | zip + `holdexar_seed.db.gz` + `holdexar_template.db` + `latest.json` | 给用户下载的那一条,带 changelog |
| 更新清单 | `updater` | 只有 `latest.json` | 机器读的那一条,地址恒定 |

要点:

- 清单 Release **必须不是 Latest release**(`gh release create ... --latest=false`,脚本已处理)。否则 `releases/latest` 与 RSS 会被带偏,源码用户取种子会失效。
- 资产名固定为 `Holdexar-win64-v<版本>.zip`(不带时间戳),渠道自动跟进(Scoop 的 `checkver` / `autoupdate`)才能按版本号拼出 URL。
- tag 已存在时脚本改走 `gh release upload --clobber`,重复发布会覆盖资产而非失败。
- 客户端检查链:清单(镜像链)→ 失败回落 GitHub API。没发过时功能照常,只是享受不到免限速与内置 SHA256。

没装 gh:`--dry-run` 仍可打印全部命令;脚本还会给出网页端手工创建的照抄清单。

## 项目结构

```
Holdexar/
├── run.py            # 一键启动器
├── desktop/          # pywebview 壳 + PyInstaller spec
├── scripts/          # 发布脚本(出包 / 清单 / 发布 / 取种子)
├── packaging/scoop/  # Scoop 渠道用法(清单一并构建产出,不入库)
├── server/app/
│   ├── core/         # 配置 / 数据库 / 调度器 / 更新器 / 数据目录判定
│   ├── crawler/      # 爬核(aiohttp / 路由 / worker / 写库)
│   └── domains/      # 业务域(account/games/wishlist/family/bills/proxies/monitoring/...)
├── server/tests/     # 行为测试套件
└── web/src/          # Vue 3 + TS(components/ui = Hl* 体系, views = 页面)
```

## 贡献指南

欢迎提 Issue 与 PR，也欢迎 fork 后按自己的需求改造。

### 环境准备

1. 克隆仓库，启用提交门禁：`git config core.hooksPath .githooks`
2. 后端：Python ≥ 3.13，依赖与启动见上文【源码运行】（`run.py` 自动建 venv 装依赖）
3. 前端：Node ≥ 20.19（或 ≥ 22.12），`cd web && npm install`

### 开发流程

| 环节 | 命令 |
|---|---|
| 联调开发 | `python run.py --dev`（对接 Vite 热更新） |
| 仅起后端 | `python run.py --server`，浏览器访问 http://127.0.0.1:28765 |
| 后端选测 | `python scripts/test_select.py --run`（按导入关系自动圈定受影响测试） |
| 推送前冒烟 | `python scripts/test_select.py --smoke --run` |
| 后端全量 | `cd server && python -m pytest`（归发布门，日常不必跑） |
| 前端检查 | `cd web && npm run lint`、`npm run test` |
| 出包发布 | 见上文【打包与发布】 |

### 提交约定

- 一个提交只做一件事；提交信息一句话功能概述，单行，不加正文
- pre-commit 会自动跑七道门禁：敏感面扫描、注释留痕、写路径审计、前端 lint、路由完整性、双语词典、旗帜素材。任何一项未过先修内容，禁止 `--no-verify` 绕行
- 数据库写入统一走全局写调度器（write_gate），例外需先登记；前端新代码一律使用 Hl\* 组件体系，不直连 Element Plus
- PR 聚焦单一功能面，正文写清做了什么、怎么验证的（测试或实测结果）

### 常见定制点

| 定制项 | 位置 |
|---|---|
| 区服列表（增删国家） | `server/app/crawler/config.py` → `CC_LIST` |
| 调度节奏（任务 / 间隔） | `server/app/core/scheduler.py` → `start_scheduler()` |
| 爬取并发与超时 | `server/app/crawler/config.py` → `DEFAULT_WORKER_COUNT` / `HTTP_TIMEOUT` |
| 主题与样式 | `web/src/styles/`（token 化，深浅主题各一套） |

## 致谢

- [mihomo](https://github.com/MetaCubeX/mihomo) — Clash 代理内核（外部按需组件，GPL-3.0）
