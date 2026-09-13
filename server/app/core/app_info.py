"""应用元信息：唯一的品牌常量来源（改名只改这里）。

APP_NAME  → 界面标题 / FastAPI title / 窗口标题
APP_SLUG  → 数据库文件名 / 日志文件名 / 数据目录名 / 环境变量前缀
GITHUB_REPO → 发布仓库（应用自更新 + 资产种子获取共用，fork 后只改这里）
MANIFEST_*  → 更新清单的固定落点（客户端检查更新与发布脚本共用同一地址）
"""

APP_NAME = "Holdexar"
APP_SUBTITLE = "Steam 多区价格监控终端"
APP_SLUG = APP_NAME.lower()

# 版本号：**全项目唯一来源**（config.Settings.version 只是把它接进 pydantic 设置，
# 便于环境变量覆盖；发布脚本读的也是这里）。发版时只改这一行。
# 放在 app_info 而非 config：config 要 import pydantic，而发布/构建脚本、
# run.py、desktop/main.py 需要在装依赖之前就能取到版本号。
APP_VERSION = "0.2.0"

# 发布仓库（owner/repo）：更新检查与种子资产下载共用一个来源，勿在两处各写一遍
GITHUB_REPO = "GLrone/Holdexar"

# 更新清单落在一个**固定 tag** 的 Release 资产下，与版本号解耦：
# 客户端检查更新永远只读这一个地址，不打 api.github.com（免限速、国内可达）。
# 发布端的对应动作在 scripts/publish_release.py（刷该 tag 的资产）。
MANIFEST_TAG = "updater"
MANIFEST_ASSET = "latest.json"
MANIFEST_SCHEMA = 1

# 环境变量前缀（pydantic-settings 用；改品牌时如需改前缀在此同步）
ENV_PREFIX = "HOLDEXAR_"
