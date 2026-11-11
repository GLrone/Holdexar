# Go 原生发行包构建：四件套 + 标准目录布局 + zip（无 Python 参与）。
# 用法：powershell -File scripts\build_release_go.ps1 [-Version 0.3.6-beta] [-OutDir release-go]
# 布局（解压后）：
#   Holdexar.exe                     根启动器（go_launcher）
#   state/current.json               初始指针
#   updater/UpdateHost.exe           原生更新宿主
#   versions/<v>/Holdexar.exe        原生窗口宿主
#   versions/<v>/_internal/serverd.exe + seed/
#   web/dist/                        前端产物（安装根级，服务端同源解析）
#   assets/seed/...                  随包基础资料（miniseed 候选位）
param(
    [string]$Version = "",
    [string]$OutDir = "release-go",
    [string]$RepoRoot = "",
    [switch]$SkipWeb
)
$ErrorActionPreference = 'Stop'
if (-not $RepoRoot) { $RepoRoot = Split-Path -Parent $PSScriptRoot }
# OutDir 转绝对：Build-Go 在模块目录内以相对 -o 落盘，产物会散进各模块目录
$OutDir = [System.IO.Path]::GetFullPath((Join-Path (Get-Location).Path $OutDir))

if (-not $Version) {
    $versionLine = Select-String -Path (Join-Path $RepoRoot 'server-go\internal\config\config.go') -Pattern 'AppVersion\s*=\s*"([^"]+)"' | Select-Object -First 1
    if (-not $versionLine) { throw '无法从 config.go 读取版本号，请用 -Version 显式指定' }
    $Version = $versionLine.Matches[0].Groups[1].Value
}
Write-Output "[pack] 版本 $Version"

$Build = Join-Path $OutDir 'build'
$App = Join-Path $Build 'Holdexar'
$VerDir = Join-Path $App ("versions\" + $Version)
if (Test-Path $Build) { Remove-Item -Recurse -Force $Build }
New-Item -ItemType Directory -Force -Path $VerDir, (Join-Path $VerDir '_internal'), (Join-Path $App 'updater'), (Join-Path $App 'state') | Out-Null

function Build-Go([string]$Dir, [string]$Out, [string]$LdFlags, [string]$Pkg = ".") {
    $outParent = Split-Path -Parent $Out
    if ($outParent -and -not (Test-Path $outParent)) { New-Item -ItemType Directory -Force -Path $outParent | Out-Null }
    Push-Location $Dir
    $env:GOFLAGS = '-mod=mod'
    $env:GOPROXY = 'off'
    # PS 5.1 会丢弃空串实参（-ldflags '' 会吞掉 -o）：非空时才传
    if ($LdFlags) {
        $output = go build -trimpath -ldflags $LdFlags -o $Out $Pkg 2>&1
    } else {
        $output = go build -trimpath -o $Out $Pkg 2>&1
    }
    $code = $LASTEXITCODE
    Pop-Location
    if ($code -ne 0) { throw ("go build 失败（$Dir）： " + ($output -join ' | ')) }
}

Write-Output '[pack] 构建根启动器'
Build-Go (Join-Path $RepoRoot 'desktop\go_launcher') (Join-Path $App 'Holdexar.exe') '-s -w -H=windowsgui'
Write-Output '[pack] 构建窗口宿主'
Build-Go (Join-Path $RepoRoot 'desktop\windowhost') (Join-Path $VerDir 'Holdexar.exe') '-s -w -H=windowsgui'
Write-Output '[pack] 构建本地服务'
Build-Go (Join-Path $RepoRoot 'server-go') (Join-Path $VerDir '_internal\serverd.exe') ("-X holdexar/server-go/internal/config.AppVersion=" + $Version) "./cmd/serverd"
Write-Output '[pack] 构建更新宿主'
Build-Go (Join-Path $RepoRoot 'desktop\updatehost') (Join-Path $App 'updater\UpdateHost.exe') '-s -w'

if (-not $SkipWeb) {
    Write-Output '[pack] 前端构建'
    Push-Location (Join-Path $RepoRoot 'web')
    npm run build
    if ($LASTEXITCODE -ne 0) { Pop-Location; throw 'web build 失败' }
    Pop-Location
}
$dist = Join-Path $RepoRoot 'web\dist'
if (-not (Test-Path (Join-Path $dist 'index.html'))) { throw 'web/dist/index.html 缺失' }
Write-Output '[pack] 拷贝 web/dist 与随包资料'
Copy-Item -Recurse -Force $dist (Join-Path $App 'web\dist')
$seedSrc = Join-Path $RepoRoot 'assets\seed\holdexar_mini_seed.db'
if (Test-Path $seedSrc) {
    New-Item -ItemType Directory -Force -Path (Join-Path $VerDir '_internal\seed'), (Join-Path $App 'assets\seed') | Out-Null
    Copy-Item $seedSrc (Join-Path $VerDir '_internal\seed\holdexar_mini_seed.db')
    Copy-Item $seedSrc (Join-Path $App 'assets\seed\holdexar_mini_seed.db')
}

# 版本目录自带前端：跨运行时换装只装版本目录，根级 web/dist 在旧版安装里
# 不存在——服务端按根级优先、版本目录回退解析，任何安装路径都自洽
Write-Output '[pack] 版本目录携带 web/dist'
Copy-Item -Recurse -Force $dist (Join-Path $VerDir 'web\dist')

Write-Output '[pack] 初始指针'
$stateJson = @{ version = $Version; installedAt = 0; lastKnownGood = $null } | ConvertTo-Json -Compress
[IO.File]::WriteAllText((Join-Path $App 'state\current.json'), $stateJson)

Write-Output '[pack] 校验关键件'
foreach ($f in @(
    (Join-Path $App 'Holdexar.exe'),
    (Join-Path $App 'updater\UpdateHost.exe'),
    (Join-Path $VerDir 'Holdexar.exe'),
    (Join-Path $VerDir '_internal\serverd.exe'),
    (Join-Path $App 'web\dist\index.html'))) {
    if (-not (Test-Path $f)) { throw "关键件缺失：$f" }
}

Write-Output '[pack] 打包 zip'
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$zip = Join-Path $OutDir ("Holdexar-win64-v" + $Version + ".zip")
if (Test-Path $zip) { Remove-Item -Force $zip }
Compress-Archive -Path $App -DestinationPath $zip -CompressionLevel Optimal
$zipHash = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
$zipSize = (Get-Item $zip).Length
[IO.File]::WriteAllText((Join-Path $OutDir 'sha256.txt'), "$zipHash  $(Split-Path -Leaf $zip)`n")
Write-Output ("[pack] 完成：{0}（{1:N0} 字节，sha256 {2}）" -f $zip, $zipSize, $zipHash)
