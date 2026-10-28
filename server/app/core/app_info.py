"""应用元信息：唯一的品牌常量来源（改名只改这里）。

APP_NAME  → 界面标题 / FastAPI title / 窗口标题
APP_SLUG  → 数据库文件名 / 日志文件名 / 数据目录名 / 环境变量前缀
GITHUB_REPO → 发布仓库（应用自更新 + 资产种子获取共用，fork 后只改这里）
MANIFEST_*  → 更新清单的固定落点（客户端检查更新与发布脚本共用同一地址）
"""

import re

APP_NAME = "Holdexar"
APP_SUBTITLE = "Steam 多区价格监控终端"
APP_SLUG = APP_NAME.lower()

# 版本号：**全项目唯一来源**（config.Settings.version 只是把它接进 pydantic 设置，
# 便于环境变量覆盖；发布脚本读的也是这里）。发版时只改这一行。
# 放在 app_info 而非 config：config 要 import pydantic，而发布/构建脚本、
# run.py、desktop/main.py 需要在装依赖之前就能取到版本号。
APP_VERSION = "0.3.6-beta"

GITHUB_REPO = "GLrone/Holdexar"

# GitHub Release 资产下载镜像链（空串 = 直连 GitHub 兜底）。
# 供 updater 与 desktop 壳共用，唯一事实源。
GITHUB_MIRRORS = ("https://ghfast.top/", "https://gh-proxy.com/", "https://gh-proxy.net/", "")

# 更新清单落在一个**固定 tag** 的 Release 资产下，与版本号解耦：
# 客户端检查更新永远只读这一个地址，不打 api.github.com（免限速、国内可达）。
# 发布端的对应动作在 scripts/publish_release.py（刷该 tag 的资产）。
MANIFEST_TAG = "updater"
MANIFEST_ASSET = "latest.json"
# schema 2 起：清单附 Ed25519 签名（signature 字段），客户端只认内嵌公钥
# （core/update_signing.TRUSTED_PUBKEYS）。schema 1 的旧客户端忽略新字段，
# 字段集保持向后兼容。
MANIFEST_SCHEMA = 2

# 换装暂存落点与交接标记：desktop/main.py 与 app/core/updater.py 共用一份——
# 写标记的一方与判标记的一方各写一套字符串，必然漂移成「落下去了但没判住」。
# manifest = 暂存就绪待换装；unsupported = 该暂存包不支持安全换装（二者在暂存目录内）；
# swap_failed = 上次换装失败并已回滚，同一安装不再自动换装——它描述的是**安装状态**
# 而不是某个包，所以落在**数据目录**：换装进程跑在暂存目录里删不掉自己，
# 而作废清理要等下一次启动由现装进程执行，标记不能被那次清理带走。
STAGING_DIR_NAME = "update-staging"
STAGING_MANIFEST_NAME = "manifest.json"
HANDOFF_UNSUPPORTED_MARK = ".handoff-unsupported"
SWAP_FAILED_FLAG = ".swap-failed"

# 环境变量前缀（pydantic-settings 用；改品牌时如需改前缀在此同步）
ENV_PREFIX = "HOLDEXAR_"

# 交接账本：现装进程每次把换装交给暂存包前登记（tag + 次数），同一暂存包第二次
# 交接被拒。换装失败回滚后现装又被拉起，若只看 manifest 就再交一次，任何一版
# 暂存包（包括没有摘除 manifest 逻辑的旧构建）都能把它拖成死循环——终止判据
# 必须落在现装侧，不能指望暂存包自己守规矩。换装成功/用户重新下载时清零。
HANDOFF_LEDGER_NAME = "update-handoff.json"

# 健康检查超时阈值（秒）。换装验收对象是**新拷贝落盘、从未运行过**的二进制：
# 冷启动含杀软同步扫描与建库导入，须与桌面壳自身的首跑等待预算（desktop/main
# 的 90s）同量级——压得过低会把「还在启动」误判为「不健康」，好更新被回滚。
HEALTH_TIMEOUT = 90.0


def version_from_tag(tag: str) -> str:
    """release tag → 版本号（v0.3.0-beta → 0.3.0-beta）。"""
    t = str(tag or "").strip()
    if (t.startswith("v") or t.startswith("V")) and len(t) > 1 and t[1].isdigit():
        return t[1:]
    return t


def version_gt(a: str, b: str) -> bool:
    """a 是否比 b 新。

    主段按点分数字逐段比（0.1.0 < 0.2.0 < 0.10.0），长度不齐补 0（0.2 == 0.2.0）。
    主段相同时按预发布段判：**有预发布段的一侧更旧**（0.1.0-beta.2 < 0.1.0），
    两侧都有则逐标识比（0.1.0-alpha < 0.1.0-beta.2 < 0.1.0-rc.1），
    同前缀下标识少的更旧（0.1.0-alpha < 0.1.0-alpha.1）。

    住在 app_info 这个零依赖模块里：桌面壳打包态走 PYZ、发布脚本在裸环境跑，
    都不便为版本比较拖进 httpx 级依赖。
    """
    rel_a, pre_a = _version_parts(a)
    rel_b, pre_b = _version_parts(b)

    length = max(len(rel_a), len(rel_b))
    rel_a += [0] * (length - len(rel_a))
    rel_b += [0] * (length - len(rel_b))
    if rel_a != rel_b:
        return rel_a > rel_b

    if not pre_a and not pre_b:
        return False
    if not pre_a:
        return True  # a 是正式版、b 是预发布 → a 更新
    if not pre_b:
        return False  # a 是预发布、b 是正式版 → a 更旧

    for got, want in zip(pre_a, pre_b):
        if got != want:
            return got > want
    return len(pre_a) > len(pre_b)


def _version_parts(version: str) -> tuple[list[int], list[tuple[int, object]]]:
    """版本号 → (主段数字, 预发布段)。

    主段只取数字（`v0.2` 与 `0.2` 同解）；预发布段在 `-` 之后、`+` 之前，
    数字标识按数值比、字母标识按字典序比——两者分属不同类型，比较时先看类型位。
    """
    text = version.strip()
    if "+" in text:
        text = text.split("+", 1)[0]
    release_text, _, pre_text = text.partition("-")

    release: list[int] = []
    for seg in release_text.split("."):
        digits = re.sub(r"\D", "", seg)
        release.append(int(digits) if digits else 0)

    pre: list[tuple[int, object]] = []
    if pre_text:
        for ident in pre_text.split("."):
            if ident.isdigit():
                pre.append((0, int(ident)))
            elif ident:
                pre.append((1, ident.lower()))
    return release, pre
