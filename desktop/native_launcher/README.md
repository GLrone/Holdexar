# Native Launcher Prototype

原生轻量启动器原型（Go 实现）。

## 构建

无需外部依赖与 CGO，仅依赖 Go 标准库：

```powershell
go build -ldflags="-s -w -H=windowsgui" -o Holdexar.exe main.go
```

## 行为契约

1. 根目录与基础目录结构建立 (`state`, `data`, `logs`, `versions`, `updater`, `update-staging`)。
2. `--update-protocol` 快速查询响应 (`{"updateDataProtocol": 1}`)。
3. AppData 历史数据平滑迁移至便携式 `data/` 目录。
4. SQLite 头部 `user_version` 结构兼容性拦截。
5. `current.json` 版本定位与自动故障回退。
6. 环境变量注入与前台/后台启动模式。
