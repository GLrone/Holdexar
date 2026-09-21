<div align="center">

<br>

# Holdexar

**本地 Steam 多区价格监控终端**

[![Version](https://img.shields.io/badge/version-v0.1.0-orange)](../../releases)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue?logo=python&logoColor=white)](https://www.python.org)
[![FastAPI](https://img.shields.io/badge/Backend-FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Vue 3](https://img.shields.io/badge/Frontend-Vue%203-42b883?logo=vuedotjs&logoColor=white)](https://vuejs.org)
[![Platform](https://img.shields.io/badge/Platform-Windows%2010%2F11-0078D4?logo=windows&logoColor=white)](https://github.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

</div>

## 简介

Holdexar 在本机完成 Steam 多区价格的抓取、折算与存储:异步爬核按监控池与区服集请求 Steam 商店公开端点,统一折算 CNY,全量数据落单文件 SQLite(`holdexar.db`,默认在系统数据目录),无云端后端、无遥测。

| 层 | 组件 |
|---|---|
| 后端 | FastAPI · SQLAlchemy(async) · APScheduler(21 个定时任务) · aiohttp 爬核 |
| 前端 | Vue 3 · TypeScript · Vite · 自制 Hl\* 组件体系(深浅双主题 / 中英双语) |
| 桌面 | pywebview(WebView2) · onedir 绿色包 · 单实例锁 |
| 网络 | 出网策略引擎(direct_only / proxy_first / proxy_only) · 自备 Clash 订阅 · mihomo 内核与 GeoIP 数据随包内置 |
| 认证 | 桌面登录窗即 Steam 官方页面 |

## 主要功能

| 模块 | 功能 |
|---|---|
| 游戏库 | 41 区价格矩阵:本地货币标价 / 折扣 / 折算 CNY / 史低标记;折扣与锁区筛选 |
| 游戏详情 | 多版本(标准 / 黄金 / 命名版)价格对比;按区价格走势曲线 |
| 愿望单 | 多账户管理;愿望单 / 已购双通道同步;条目带中文名与封面 |
| 家庭组 | 共享库聚合分析 10 视图:贡献分布、入库热力、价值洞察(原价 / 实付 / 性价比)、活跃分档 |
| 账单 | 消费 / 零售许可 / 礼物许可三表同步;多币种按入账日汇率折算 CNY,多年同口径 |
| 汇率 | 39 币种兑 CNY 走势;随包 2010→2026 历史档案;卡商充值价目参考 |
| 捆绑包 | bundle / sub 双轨身份识别;无小数货币(JPY / CLP 等)minor units 正确折算;多版本价格归拢 |
| 任务 | 爬取队列可视化;手动触发 / 批量导入 / 收藏列表 JSON 导入;自动价格链开关 |
| 代理 | Clash 订阅接入与节点体检账本;节点状态机(冷却 / 淘汰 / 复活 / 废弃);走线日志 |
| 提醒 | 降价 / 历史新低自动邮件;自定义阈值条件(如「CN 区 ≤¥42」) |
| 工具箱 | CDK 批量激活:多码并发、逐码回执、按账号 30 分钟配额 |

## 核心特性

### 个性化

- 监控池入池来源:账户愿望单 / 已购自动同步、游戏卡星标关注、批量导入与收藏列表 JSON 导入(导入即入池)、畅销榜发现源反哺
- 移出监控 = 排除(不删除):对象仍在库,价格历史与来源保留;重新加入即恢复监控
- 关注条目(manual)双待遇:账户同步停用核对免疫 + 价格刷新队列最高优先级(关注 > 打折 / 史低 > 其余)
- 区服勾选集驱动全链:默认前四区(cn/ru/kz/ua),价格矩阵、筛选下拉、爬取范围随勾选集收敛
- 已购游戏可圈定独立抓取区服子集;按账户分别开关愿望单 / 已购同步
- 深浅双主题、中英双语一键切换

### 自动化

21 个定时任务常驻后台:

| 节奏 | 任务 |
|---|---|
| 1 min | 钱包余额轮转 |
| 5 min | 失败区定向修复(空闲档,不与主链抢锁) |
| 15 min | 账户同步(差异入库,新增条目即时首爬) |
| 30 min | 账单同步 · 订阅重拉(6h 门槛,占线顺延) |
| 1 h | 畅销榜发现源反哺 |
| 6 h | 全区刷价(监控池 → 库内其余两段,锚点网格对齐 Steam 折扣刷新时刻,外部时间判 DST)· 代理体检 · 特惠榜 · 限时赠送复查 |
| 24 h | 汇率刷新 · 热门新品 · 即将推出 · VACUUM INTO 在线备份 |
| 每日 04:00 | 汇率历史修复(缺口 / 配额门禁) |
| 每日 04:30 | 数据库 WAL 检查点 |
| 每日 05:20 | 成就同步 |
| 每日 05:40 | 捆绑包来源抓取 |
| 每日 06:40 | 月包选择同步 |
| 每日 07:10 | 免费游戏快照 |
| 每日 10:00 | 暂缓条目(COMING_SOON)重探,限量 20 个 |

自愈机制:

- 瞬时失败(429 / 网络抖动)进欠账账本,冷却重试 ≤5 次,穷尽转 blocked 终态
- 挂名孤儿行 60 款/日回补;新入池条目 15 分钟内首爬
- 代理节点以真实 Steam 端点体检:冷却阶梯 30min→24h,连败 10 次淘汰,复活需连续 3 轮全过,订阅 >95% 不可用标记废弃
- 关机错过定点自动兜底:汇率按快照龄启动补刷,爬价网格自动重锚
- 自动价格链可一键关停,转纯手动模式

### 私人化

- 零云端、零遥测、无账号体系:除 Steam 官方 API 与自备代理外不连接任何服务
- 凭据(Cookie / JWT / 订阅 URL)仅存本机 SQLite
- 不内置订阅源与推荐节点,出网路径由策略引擎按连通性决策
- 数据目录独立于程序目录(默认 `%LOCALAPPDATA%\Holdexar`):覆盖解压/换目录/整包重装都不碰用户数据;要便携(U 盘携带)就在程序目录放一个空的 `portable.flag`,数据即回到 exe 同级 `data/`
- 每日 VACUUM INTO 在线备份,不打断写入
- 更新检查读固定地址的更新清单(`latest.json`)而不是 GitHub API:免 60 次/小时限速、国内可达、可走镜像;换装前校验 SHA256,旧程序目录整体留作回退,换装中断自动回滚并重新打开应用

## 首次运行安全提示

> [!IMPORTANT]
> 本应用发布包**未经代码签名**，首次运行会遇到两类安全提示，均为无签名程序的正常现象，不是病毒：
>
> 1. **Windows SmartScreen 弹窗**（「Windows 已保护你的电脑」）：点 **更多信息 → 仍要运行** 即可。
> 2. **杀毒软件报毒 / 主动防御弹窗**（如「正在创建 BITS 任务」）：选 **允许 / 信任**，不要阻止——阻止可能影响窗口渲染。建议把整个程序目录加入杀软白名单。
>
> 请只从官方 [Releases](../../releases) 页下载，并用页内公布的 SHA256 校验安装包。本应用零云端、零遥测、无账号体系，全部数据仅落本机 SQLite（详见下文「私人化」）；未签名的成因与完整处置见文末 [FAQ](#faq)。

## 下载安装(绿色版)

1. [Releases](../../releases) 下载 `Holdexar-win64-v<版本>.zip`
2. 解压,双击 `Holdexar/Holdexar.exe`
3. 上手:【代理】接入 Clash 订阅 →【设置】桌面登录绑定账号 →【任务】导入监控目标

依赖 WebView2 Runtime(Win11 自带,缺失自动引导安装)。

**Scoop 渠道**(便携应用,不写注册表、不装服务):

```powershell
scoop bucket add holdexar https://github.com/GLrone/scoop-bucket
scoop install holdexar
```

**升级**:有新版本时启动会主动提示一次,侧栏「我」项常驻红点,顶栏同时出现更新胶囊
(发现新版 / 下载中 / 待重启三态;悬停看更新内容,点击打开更新报告窗口,下载中胶囊
显示百分比)。进【我 → 应用更新】或点胶囊即可检查并一键升级(下载 → SHA256 校验 →
重启换装);更新报告窗口内含发版说明、发布日期与「自动下载」开关。换装只替换程序
文件,用户数据、账号绑定、价格历史一概不动;若换装中断会回滚到原版本并自动重新打开
应用。也可以直接下载新版解压覆盖或换个目录解压,数据都不受影响。
杀软误报为无签名包常见现象，处置方式见文末 [FAQ](#faq)。

每个版本 Release 附三件资产:

| 资产 | 用途 |
|---|---|
| `Holdexar-win64-v<版本>.zip` | 应用包,解压即用 |
| `holdexar_seed.db` | 公共数据种子(汇率历史 / 价格历史切片;仅源码运行需要,发布包已内置) |
| `latest.json` | 更新清单,客户端检查更新读取 |

## 源码运行

要求 Python ≥3.11、Node ≥ 20.19(或 ≥ 22.12;Vite 8 的 `engines` 要求):

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

首次运行会自动补齐公共数据种子(16 年历史汇率与价格历史切片,约 288MB):种子是二进制大文件,不进 git,由 `run.py` 从 Release 资产拉取。没网也不影响启动,只是【汇率】页没有历史档案,之后重跑 `python scripts/fetch_seed.py` 即可。

随包的还有 Clash 内核(mihomo + GeoIP 数据,约 72MB,版本固定):同为二进制不入库,由 `run.py` 调用 `python scripts/fetch_kernel.py` 从上游补齐;获取失败也不阻断启动,【代理】页会显示「内核缺失」并提供一键安装入口。

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
| 版本发布 | `v<版本>` | zip + `holdexar_seed.db` + `latest.json` | 给用户下载的那一条,带 changelog |
| 更新清单 | `updater` | 只有 `latest.json` | 机器读的那一条,地址恒定 |

要点:

- 清单 Release **必须不是 Latest release**(`gh release create ... --latest=false`,
  脚本已处理)。否则 `releases/latest` 与 RSS 会被带偏,源码用户取种子会失效。
- 资产名固定为 `Holdexar-win64-v<版本>.zip`(不带时间戳),渠道自动跟进
  (Scoop 的 `checkver` / `autoupdate`)才能按版本号拼出 URL。
- tag 已存在时脚本改走 `gh release upload --clobber`,重复发布会覆盖资产而非失败。
- 客户端检查链:清单(镜像链)→ 失败回落 GitHub API。没发过时功能照常,
  只是享受不到免限速与内置 SHA256。

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

## FAQ

<details>
<summary>首次运行弹出「Windows 已保护你的电脑」?</summary>

Windows SmartScreen 对**未经代码签名**的 exe 的标准提示，任何无签名程序首次运行都会出现，与病毒无关。点 **更多信息 → 仍要运行** 即可，同一版本之后不会再弹。安装前可对照 Release 页公布的 SHA256 校验 zip 完整性。

</details>
<details>
<summary>杀毒软件报毒 / 提示「正在创建 BITS 任务」?</summary>

无签名程序是杀软启发式误报的高发形态；随包分发的 mihomo 代理内核(开源 Clash 系内核,见文末致谢)也常被规则库标记。至于「创建 BITS 任务」:那是 WebView2 运行时(Edge 组件)自身的组件更新行为,被主动防御记到了宿主程序头上——本应用从不使用 BITS,当前版本已从桌面壳禁用 WebView2 组件更新与后台网络服务,该弹窗不再出现。

处置:把**整个程序目录**加入杀软信任区/白名单;弹窗出现时选「允许」而非「阻止」;若仍误报,可向杀软厂商提交白名单申诉(Microsoft / 360 / 火绒 / 腾讯均受理,一般 1-3 个工作日解标)。

</details>
<details>
<summary>登录窗打不开 / 二维码一直转?</summary>

登录页经代理加载,死节点自动回退直连。先到【代理】页确认存活节点;窗口附带验证指引卡。

</details>
<details>
<summary>刚装好,部分游戏无价格?</summary>

新入池条目 15 分钟内首爬(关注条目优先);历史孤儿行按 60 款/日回补;可手动触发一轮。

</details>
<details>
<summary>价格与商店页不一致?</summary>

全区刷价每 6 小时一轮,两轮之间的调价存在滞后;每条价格带抓取时间戳。

</details>
<details>
<summary>Cookie 存放与安全性?</summary>

仅存本机数据目录内的 `holdexar.db`(默认 `%LOCALAPPDATA%\Holdexar`,便携模式为程序目录 `data/`);桌面登录窗即 Steam 官方页面。数据目录等于登录态,勿外传。

</details>
<details>
<summary>不想自动爬取?</summary>

【任务】页关闭「自动价格更新」:定时爬价与失败修复停转,手动抓取不受影响,随时可再开。

</details>

## 定制与反馈

欢迎 fork 后按自己的需求改造。常见定制点:

| 定制项 | 位置 |
|---|---|
| 区服列表(增删国家) | `server/app/crawler/config.py` → `CC_LIST` |
| 调度节奏(任务 / 间隔) | `server/app/core/scheduler.py` → `start_scheduler()` |
| 爬取并发与超时 | `server/app/crawler/config.py` → `DEFAULT_WORKER_COUNT` / `HTTP_TIMEOUT` |
| 主题与样式 | `web/src/styles/`(token 化,深浅主题各一套) |

有问题或功能建议,随时提 Issue。参与开发:克隆后执行 `git config core.hooksPath .githooks` 启用提交门禁(敏感面 / 注释留痕 / lint / 路由 / 词典 / 旗帜素材六项);依赖与启动见上文【源码运行】,出包与发布见【打包与发布】。

## 致谢

- [mihomo](https://github.com/MetaCubeX/mihomo) — Clash 代理内核（随包分发，GPL-3.0）
