# windowhost —— Holdexar 原生窗口宿主（Go）

自有 Win32 主窗内嵌 WebView2，承载本地 Go 服务子进程 `serverd.exe`，替代
Python 桌面壳。发行形态：`versions/<版本>/Holdexar.exe`（本程序）+
`_internal/serverd.exe`，根启动器 `desktop/go_launcher` 负责版本选择与
数据目录注入；本程序消费 `HOLDEXAR_INSTALL_ROOT` / `HOLDEXAR_DATA_DIR` /
`WEBVIEW2_USER_DATA_FOLDER` 环境变量。

## 行为契约（对齐现役桌面壳）

- 单实例：锁端口（`HOLDEXAR_LOCK_PORT`，缺省 28965，备位 28966/28967/28968/29965/29966）
  + 健康探活身份校验（应用名 + 数据目录一致才算同库实例）；同库重复启动唤醒原窗口，
  不同隔离目录并行互不唤醒。激活协议沿锁端口：`show` 置前 / `background` 转后台常驻。
- 会话：服务端健康就绪后把一次性引导票据写入 `<数据目录>/server-bootstrap.ticket`，
  窗口读取后导航 `/api/v1/session/bootstrap?nonce=…`；票据不进日志（查询串脱敏）。
- 导航白名单：仅放行本实例确切本机源；外链（含 target=_blank 新窗）一律转交系统浏览器。
- 桌面桥：`window.pywebview.api.restart_app`（更新换装重启），由文档创建即注入的脚本合成。
- 托盘：显示主窗口 / 退出；双击唤起。关闭意图弹窗：最小化到托盘 / 退出程序 / 取消，
  页面侧幕布经 `window.__hlxCloseCurtain` 联动，主题跟随网页 dark/light。
- 退出链：收窗撤托盘 → 关闭子进程标准输入（EOF → 服务端优雅关闭，回收调度/推流/
  代理子进程）→ 等端口释放；宿主被硬杀时子进程凭同一 EOF 自动退出。

## 开发

- `dev-go-native.bat`（仓库根）：从当前源码构建两件到 `desktop/windowhost/bin/`，
  以开发指针数据目录与 28767/28966 端口启动；`HOLDEXAR_DEV_WATCH` 使 serverd
  监视源码自动重建，窗口按当前路由重载。
- 与 Python 版双开对照：`dev.bat`（Python 壳 + Go 后端）与本入口使用不同数据
  目录与端口，可同时运行。
- 测试：`go test ./...`（纯逻辑层：端口协商、身份校验、导航白名单、票据、桥协议、
  数据目录优先级，外链出口可注入）。

## 构建

```
go build -ldflags "-H=windowsgui" -o windowhost.exe .
```

依赖 `github.com/wailsapp/go-webview2`（MIT），WebView2Loader 以资源内嵌，
无随包 DLL；运行时依赖系统 WebView2 Runtime（Evergreen），缺失时弹窗给出
官方安装指引。
