# Windows Launcher Entry Experiments

本目录的 Go 是转交独立 Python 启动保护核心的实验入口。Rust 的原生正常启动链与异常更新恢复边界见 [Rust 实验说明](../rust_launcher/README.md)。默认发行构建仍使用 Python 启动器；这些入口未替换正式构建。

## 安装布局与职责

```text
Holdexar/
  Holdexar.exe
  .launcher/
    SafetyHost.exe
  versions/
  updater/
  state/
  data/
```

`Holdexar.exe` 为 Go 或 Rust 入口；`.launcher/SafetyHost.exe` 是独立打包的 Python 启动保护核心，不依赖版本目录中的主程序。它保留现役 Python 启动器的数据保护、迁移、版本选择、回退与更新恢复职责。独立 UpdateHost 继续处理更新事务。

原生入口只负责从自身映像定位安装根、拒绝保护核心缺失或路径重定向、传递参数和环境、设置 `HOLDEXAR_INSTALL_ROOT`、等待并返回保护核心的退出码。`--server`、`--background` 和 `--update-protocol` 均原样转交，不由原生入口另行解释。保护核心缺失时拒绝启动，不回退执行版本目录中的程序。

性能与体积比较必须包含原生入口和 Python 保护核心的全部开销。单独的入口响应、进程创建或协议响应耗时不代表主界面可操作时间；不得把这些入口表述为独立实现全部保护能力的纯原生启动器。

## 编译

以下命令从仓库根目录执行，输出到独立目录。Go 入口仅依赖标准库，不需要 CGO：

```powershell
$out = Join-Path $env:TEMP 'holdexar-launcher-check'
New-Item -ItemType Directory -Force -Path "$out/go", "$out/rust" | Out-Null
go build '-ldflags=-s -w -H=windowsgui' -o "$out/go/Holdexar.exe" desktop/native_launcher/main.go
```

Rust 使用 Cargo 构建；依赖、平台 SQLite 与恢复组件要求见 [Rust 实验说明](../rust_launcher/README.md)。

编译入口不会生成 `SafetyHost.exe` 或完整安装包。真实启动比较还需为各组准备同一份保护核心、版本目录与隔离数据副本。

## 入口契约自检

先生成测试用保护核心，再对两个入口运行同一组 Windows 黑盒检查：

```powershell
go test -v desktop/native_launcher/main.go desktop/native_launcher/main_test.go
go test -c -o "$out/helper-fixture.exe" desktop/native_launcher/main.go desktop/native_launcher/main_test.go
python desktop/native_launcher/verify.py --launcher "$out/go/Holdexar.exe" `
  --fixture "$out/helper-fixture.exe" --output "$out/go-check"
python desktop/native_launcher/verify.py --launcher "$out/rust/Holdexar.exe" `
  --fixture "$out/helper-fixture.exe" --output "$out/rust-check" --design full
```

检查覆盖保护核心缺失、目录冒充、junction 逃逸、中文和空格安装路径、空参数、引号、末尾反斜杠、环境继承及退出码传递。测试 fixture 仅验证入口交接，不代表真实 Python 保护核心的数据或更新安全契约通过，也不代表 GUI 启动性能验收。
