# Scoop 分发渠道

[Scoop](https://scoop.sh) 是 Windows 上的命令行包管理器，专为**便携应用**设计：
不写注册表、不装服务、解压到 `~/scoop/apps/`，`scoop uninstall` 即干净移除。
Holdexar 本身就是 PyInstaller onedir 便携包（解压即用、无安装器），是 Scoop 的
典型目标形态——所以这是一条成本极低、收益明确的分发渠道。

## 用户怎么装

```powershell
scoop bucket add holdexar https://github.com/GLrone/scoop-bucket
scoop install holdexar
```

装完开始菜单出现快捷方式，`holdexar` 可直接在终端唤起。

## 清单在哪

`packaging/scoop/` **不放清单本体**——清单里的 `version` 与 `hash` 每个版本都变，
提交进源码仓库等于每发一版就要改一次、还容易忘记导致渠道版本落后。

清单由构建流程生成：

```bash
python scripts/build_release.py     # 出包（内含 build_manifest.py）
# 或单独生成清单
python scripts/build_manifest.py
```

产物落 `release/scoop/holdexar.json`，内容形如：

```json
{
  "version": "0.1.0",
  "architecture": {
    "64bit": {
      "url": "https://github.com/GLrone/Holdexar/releases/download/v0.1.0/Holdexar-win64-v0.1.0.zip",
      "hash": "<zip 的 sha256>",
      "extract_dir": "Holdexar"
    }
  },
  "shortcuts": [["Holdexar.exe", "Holdexar"]],
  "checkver": { "url": ".../releases/download/updater/latest.json", "jsonpath": "$.version" },
  "autoupdate": { ... }
}
```

两个关键点：

- **`extract_dir: "Holdexar"`** —— zip 内根目录是 `Holdexar/`，不下钻会把
  整个目录结构散在 `apps/holdexar/current/` 下。
- **`checkver` 直接读我们的更新清单** —— Scoop 检查渠道版本时不打
  `api.github.com`，和客户端检查更新走的是同一个出口（`updater` tag 下的
  `latest.json`）。免 API 限速，且国内可达。

## 怎么发布新版本

```bash
python scripts/build_release.py                      # 出包 + 生成清单
python scripts/publish_release.py --scoop-dir <bucket 仓库本地路径>
```

`--scoop-dir` 会把生成好的清单写进 bucket 仓库的 `bucket/` 目录，
之后在 bucket 仓库里 commit + push 即可。用户下次 `scoop update` 就能拿到新版。

## bucket 仓库怎么建

独立一个仓库（Scoop 约定名 `scoop-bucket`，仓库根下放 `bucket/` 目录）：

```
GLrone/scoop-bucket
└── bucket/
    └── holdexar.json     ← publish_release.py --scoop-dir 写入的就是这里
```

仓库只需要一个 `README.md` 和这个 `bucket/` 目录，无需任何构建配置。

## 备选：提交到官方 Extras

如果希望用户不额外加 bucket 就能装（`scoop install extras/holdexar`），
可以向 [ScoopInstaller/Extras](https://github.com/ScoopInstaller/Extras) 提 PR。
官方清单要求更严（需有稳定下载地址、`license` 字段规范、通过
`checkver.ps1` 自检），且后续每次发版需要维护 PR。建议先跑通自有 bucket，
等发布节奏稳定后再考虑上官方。

## 与其它渠道的关系

| 渠道 | 适合谁 | 状态 |
|---|---|---|
| GitHub Releases 便携 zip | 所有人（手动下载） | 已有 |
| 应用内更新（读 `latest.json`） | 已安装用户 | 已有 |
| Scoop | 习惯命令行管包的开发者 | 本目录 |
| winget | 需要系统级安装（开始菜单/卸载项）的用户 | 需先做安装器，暂缓 |
