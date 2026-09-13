"""资产种子获取：从 GitHub Release 资产下载 holdexar_seed.db 到 assets/seed/。

**为什么种子不进 git**：种子 ≈ 12MB 二进制，且每次发布重导都会变——入库等于
每个版本往历史里永久塞一份，几十个版本后仓库不可用。所以它走 Release 资产
（发布包内置一份 + 资产独立一份），源码 clone 用户由 run.py 首次启动时调用
本脚本自动补齐。

种子内容 = 公共数据快照：**16 年汇率档案（2010-04 起，23 万行）** + games
人工策划列。结构上不含任何用户凭据（白名单导出，见 scripts/export_seed.py）。

用法（源码运行）：
    python scripts/fetch_seed.py            # 缺则下载；已有且有效则跳过
    python scripts/fetch_seed.py --force    # 强制重下
发布机：不需要本脚本（build_release.py --refresh-seed 从生产库现导）。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from app.core.app_info import GITHUB_REPO  # noqa: E402

SEED_DIR = ROOT / "assets" / "seed"
SEED_DB = SEED_DIR / "holdexar_seed.db"
ASSET_NAME = "holdexar_seed.db"

# 通道链：先直连，再走国内镜像（GitHub Release 资产直连在国内基本不可用）。
# 与 app/core/updater.py 的镜像链同源，改动请两边对齐。
_MIRRORS = ("", "https://ghfast.top/", "https://gh-proxy.com/")

_TIMEOUT_S = 120


def asset_url(mirror: str = "") -> str:
    """种子地址：`releases/latest/download/` 恒定指向**最新正式版**发布。

    更新清单 Release（tag `updater`）以 `--latest=false` 创建，不会被标成
    Latest release，所以这里的 latest 不会被它顶掉——这条地址可以长期不变。
    """
    return f"{mirror}https://github.com/{GITHUB_REPO}/releases/latest/download/{ASSET_NAME}"


def seed_ok(path: Path) -> tuple[bool, str]:
    """种子有效性：能只读打开 + 有 seed_meta + 汇率档案非空。

    坏种子等同无种子（后端 read_seed_meta 会静默忽略），此处提前拦下，
    避免把半截下载文件留在磁盘上冒充可用种子。
    """
    if not path.is_file():
        return False, "文件不存在"
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            meta = dict(con.execute("SELECT key, value FROM seed_meta").fetchall())
            rows = con.execute("SELECT COUNT(*) FROM fx_rate_history").fetchone()[0]
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001 —— 任何异常都算坏档
        return False, f"读取失败：{e}"
    if rows <= 0:
        return False, "汇率档案为空"
    desc = f"version={meta.get('version', '?')} 汇率档案 {rows} 行"
    if meta.get("rows_history"):
        desc += f" / 价格历史 {meta['rows_history']} 行"
    return True, desc


def _download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "Holdexar-seed-fetcher"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp, dest.open("wb") as f:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)


def fetch(force: bool = False) -> int:
    """下载种子。返回进程退出码（0 成功或已就绪，1 失败）。"""
    if not force:
        ok, detail = seed_ok(SEED_DB)
        if ok:
            print(f"[种子] 已就绪（{detail}），跳过")
            return 0

    SEED_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SEED_DB.with_suffix(".part")
    last_err = ""
    for mirror in _MIRRORS:
        url = asset_url(mirror)
        label = mirror or "直连"
        print(f"[种子] 下载（{label}）…")
        try:
            tmp.unlink(missing_ok=True)
            _download(url, tmp)
            ok, detail = seed_ok(tmp)
            if not ok:
                last_err = f"{label}：下载内容无效（{detail}）"
                continue
            tmp.replace(SEED_DB)
            size_mb = SEED_DB.stat().st_size / 1048576
            print(f"[种子] 完成：{SEED_DB}（{size_mb:.1f} MB，{detail}）")
            return 0
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last_err = f"{label}：{e}"
            continue

    tmp.unlink(missing_ok=True)
    print(
        f"[种子] 获取失败（{last_err}）。\n"
        f"       汇率历史档案暂缺，应用其余功能不受影响；联网后可重跑本脚本，"
        f"或手动取一份放到 {SEED_DB}\n"
        f"       （发布包内的 Holdexar/_internal/seed/{ASSET_NAME} 即同一文件，可直接拷用）"
    )
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Holdexar 资产种子获取")
    parser.add_argument("--force", action="store_true", help="已有种子也强制重下")
    args = parser.parse_args()
    sys.exit(fetch(force=args.force))


if __name__ == "__main__":
    main()
