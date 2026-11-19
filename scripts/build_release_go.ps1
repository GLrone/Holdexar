# Go 原生发行包构建：输出至 <OutDir>/build/Holdexar 并打包为 Holdexar-win64-v<Version>.zip。
param(
    [string]$Version = "",
    [string]$OutDir = "release-go",
    [string]$RepoRoot = "",
    [switch]$SkipWeb
)
$ErrorActionPreference = 'Stop'
if (-not $RepoRoot) { $RepoRoot = Split-Path -Parent $PSScriptRoot }
if (-not [System.IO.Path]::IsPathRooted($OutDir)) {
    $OutDir = Join-Path (Get-Location).Path $OutDir
}
$OutDir = [System.IO.Path]::GetFullPath($OutDir)

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
New-Item -ItemType Directory -Force -Path $VerDir, (Join-Path $App 'updater'), (Join-Path $App 'state') | Out-Null

function Build-Go([string]$Dir, [string]$Out, [string]$LdFlags, [string]$Pkg = ".") {
    $outParent = Split-Path -Parent $Out
    if ($outParent -and -not (Test-Path $outParent)) { New-Item -ItemType Directory -Force -Path $outParent | Out-Null }
    Push-Location $Dir
    $env:GOWORK = 'off'
    $env:GOFLAGS = '-mod=readonly'
    $env:GOPROXY = 'off'
    if ($LdFlags) {
        $output = go build -trimpath -ldflags $LdFlags -o $Out $Pkg 2>&1
    } else {
        $output = go build -trimpath -o $Out $Pkg 2>&1
    }
    $code = $LASTEXITCODE
    Pop-Location
    if ($code -ne 0) { throw ("go build 失败（$Dir）： " + ($output -join ' | ')) }
}

Write-Output '[pack] 构建统一窗口宿主与根入口'
Build-Go (Join-Path $RepoRoot 'desktop\windowhost') (Join-Path $VerDir 'Holdexar.exe') ("-s -w -H=windowsgui -X main.buildVersion=" + $Version)
Copy-Item -Force (Join-Path $VerDir 'Holdexar.exe') (Join-Path $App 'Holdexar.exe')

Write-Output '[pack] 构建本地服务'
Build-Go (Join-Path $RepoRoot 'server-go') (Join-Path $VerDir 'serverd.exe') ("-s -w -X holdexar/server-go/internal/config.AppVersion=" + $Version) "./cmd/serverd"

Write-Output '[pack] 构建更新宿主'
Build-Go (Join-Path $RepoRoot 'desktop\updatehost') (Join-Path $App 'updater\UpdateHost.exe') '-s -w'

Write-Output '[pack] 拷贝应用图标'
$iconSrc = Join-Path $RepoRoot 'desktop\app.ico'
if (-not (Test-Path $iconSrc)) { throw "应用图标缺失：$iconSrc" }
Copy-Item -Force $iconSrc (Join-Path $App 'app.ico')
Copy-Item -Force $iconSrc (Join-Path $VerDir 'app.ico')

if (-not $SkipWeb) {
    Write-Output '[pack] 前端构建'
    Push-Location (Join-Path $RepoRoot 'web')
    npm run build
    if ($LASTEXITCODE -ne 0) { Pop-Location; throw 'web build 失败' }
    Pop-Location
}
$dist = Join-Path $RepoRoot 'web\dist'
if (-not (Test-Path (Join-Path $dist 'index.html'))) { throw 'web/dist/index.html 缺失' }
Write-Output '[pack] 拷贝版本目录 web/dist 与离线小种子'
New-Item -ItemType Directory -Force -Path (Join-Path $VerDir 'web') | Out-Null
Copy-Item -Recurse -Force $dist (Join-Path $VerDir 'web\dist')

$seedSrc = Join-Path $RepoRoot 'assets\seed\holdexar_mini_seed.db'
if (Test-Path $seedSrc) {
    New-Item -ItemType Directory -Force -Path (Join-Path $VerDir 'seed') | Out-Null
    Copy-Item -Force $seedSrc (Join-Path $VerDir 'seed\holdexar_mini_seed.db')
}

Write-Output '[pack] 随包 mihomo 许可与来源说明'
$licSrc = Join-Path $RepoRoot 'packaging\licenses\LICENSE-mihomo.txt'
if (-not (Test-Path $licSrc)) { throw "随包许可缺失：$licSrc（GPL-3.0 要求许可原文随包，缺失不得出包）" }
$clashDir = Join-Path $App 'assets\clash'
New-Item -ItemType Directory -Force -Path $clashDir | Out-Null
Copy-Item $licSrc (Join-Path $clashDir 'LICENSE-mihomo.txt') -Force
$mihomoLine = Select-String -Path (Join-Path $RepoRoot 'server-go\internal\clashkernel\kernel.go') -Pattern 'MIHOMOVersion\s*=\s*"([^"]+)"' | Select-Object -First 1
if (-not $mihomoLine) { throw '无法从 server-go\internal\clashkernel\kernel.go 读取内核版本号' }
$mihomoRepoLine = Select-String -Path (Join-Path $RepoRoot 'server-go\internal\clashkernel\download.go') -Pattern 'mihomoRepo\s*=\s*"([^"]+)"' | Select-Object -First 1
if (-not $mihomoRepoLine) { throw '无法从 server-go\internal\clashkernel\download.go 读取内核来源地址' }
$clashReadme = @"
Holdexar 代理内核资产与许可说明（按需组件）
==================================================
mihomo         $($mihomoLine.Matches[0].Groups[1].Value)
               来源 $($mihomoRepoLine.Matches[0].Groups[1].Value)
               许可 GPL-3.0（原文见同目录 LICENSE-mihomo.txt）
mihomo 以独立进程运行（非链接、非派生），本项目自身以 GPL-3.0 分发；
内核二进制不随安装包内置，由用户按需下载或自备维护；
对应源码即上游仓库上述地址，所用二进制未做任何修改。
"@
[IO.File]::WriteAllText((Join-Path $clashDir 'README.txt'), $clashReadme, (New-Object System.Text.UTF8Encoding($false)))
foreach ($clashAsset in @('LICENSE-mihomo.txt', 'README.txt')) {
    if (-not (Test-Path (Join-Path $clashDir $clashAsset))) { throw "随包内核许可资产缺失：$clashAsset" }
}

Write-Output '[pack] 初始指针'
$stateJson = @{ version = $Version; installedAt = 0; lastKnownGood = $null } | ConvertTo-Json -Compress
[IO.File]::WriteAllText((Join-Path $App 'state\current.json'), $stateJson)

Write-Output '[pack] 校验关键件与扁平目录约束'
foreach ($f in @(
    (Join-Path $App 'Holdexar.exe'),
    (Join-Path $App 'app.ico'),
    (Join-Path $App 'updater\UpdateHost.exe'),
    (Join-Path $VerDir 'Holdexar.exe'),
    (Join-Path $VerDir 'serverd.exe'),
    (Join-Path $VerDir 'app.ico'),
    (Join-Path $VerDir 'web\dist\index.html'))) {
    if (-not (Test-Path $f)) { throw "关键件缺失：$f" }
}
foreach ($forbidden in @(
    (Join-Path $VerDir '_internal'),
    (Join-Path $App 'web\dist'),
    (Join-Path $App 'assets\seed'))) {
    if (Test-Path $forbidden) { throw "扁平布局禁止残留目录：$forbidden" }
}

Write-Output '[pack] 产物消毒：内核二进制不得随包（内核独立下载维护，许可文本随包）'
$kernelResidue = @(Get-ChildItem -Path $App -Recurse -File |
    Where-Object { $_.Name -ieq 'mihomo.exe' -or $_.Name -ieq 'mihomo' -or $_.Name -ieq 'holdexar-mihomo.exe' -or $_.Name -ieq 'holdexar-mihomo' })
if ($kernelResidue.Count -gt 0) {
    throw ("产物内发现内核二进制残留，中止出包： " + (($kernelResidue | ForEach-Object { $_.FullName }) -join '；'))
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
