# windowhost —— Holdexar 原生桌面宿主（Go）

自有 Win32 主窗内嵌 WebView2，承载本地 Go 服务子进程 `serverd.exe`，同时兼任受管安装目录的根入口 `Holdexar.exe`。

## 发行布局与启动模式

- 受管发行态：`<root>/Holdexar.exe` 与 `<root>/versions/<版本>/Holdexar.exe` 由同一 `windowhost` 模块编译（注入 `-X main.buildVersion=<版本>`），本地服务位于 `<root>/versions/<版本>/serverd.exe`，前端产物位于 `<root>/versions/<版本>/web/dist/`。
- 原地运行与回退链启：`<root>/Holdexar.exe` 启动时经 `hostkit` 校验 `state/transaction.json` 与 `state/current.json`（纯 Go 读取 SQLite 头与 WAL 的 `user_version`）；当选中版本等于 `buildVersion` 时直接在当前进程内挂载 `versions/<版本>/` 运行，仅在回滚导致版本不一致时链式拉起 `versions/<目标版本>/Holdexar.exe`。
- 单实例保护：按规范化 `dataDir` 派生确定性命名互斥体 `Local\Holdexar-WindowHost-<hex>`，唤醒监听端口写入 `<dataDir>/windowhost.port`，同库重复启动精准唤醒原窗口，多隔离数据目录可并行双开。
- 会话与安全：服务端就绪后写入 `<dataDir>/server-bootstrap.ticket`，窗口经 `/api/v1/session/bootstrap?nonce=…` 建立会话；导航仅放行本实例确切源，外链转交系统浏览器；`serverd.exe` 绑定独立 `KILL_ON_JOB_CLOSE` Job 对象。

## 开发与构建

- `dev-go.bat`（仓库根）：纯原生 Go 构建 `serverd.exe` 与 `Holdexar.exe` 至按次隔离的 `%TEMP%\holdexar-dev-bin-*` 临时目录，支持 `--server` / `--dev` / `--port` / `--data-dir` 参数透传并在退出时自动清理。
- 构建命令：

```powershell
go build -trimpath -ldflags "-s -w -H=windowsgui -X main.buildVersion=0.4.0" -o Holdexar.exe .
```
