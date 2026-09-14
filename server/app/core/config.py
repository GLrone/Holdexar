"""应用配置：环境变量 + server/.env；数据目录判定见 app.core.paths。

文件/目录命名统一走 app_info.APP_SLUG，不散落硬编码。
"""
from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.app_info import APP_NAME, APP_SLUG, APP_VERSION, ENV_PREFIX
from app.core.paths import resolve_data_dir

PROJECT_ROOT = Path(__file__).resolve().parents[3]  # server/app/core/config.py -> Holdexar/
SERVER_ROOT = PROJECT_ROOT / "server"


def _default_data_dir() -> Path:
    """数据目录：判定链见 app.core.paths.resolve_data_dir（打包态新装落系统数据目录）。"""
    return resolve_data_dir(APP_SLUG, ENV_PREFIX)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX, env_file=str(SERVER_ROOT / ".env"), extra="ignore"
    )

    app_name: str = APP_NAME
    # 版本号来源见 app_info.APP_VERSION（发版只改那一处）；此处的字段名保留
    # 是为了让 HOLDEXAR_VERSION 仍能覆盖它（打包/预发布时用得着）。
    version: str = APP_VERSION
    host: str = "127.0.0.1"
    port: int = 28765
    data_dir: Path = Field(default_factory=_default_data_dir)
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    @property
    def db_filename(self) -> str:
        return f"{APP_SLUG}.db"

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{(self.data_dir / self.db_filename).as_posix()}"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def web_dist_dir(self) -> Path:
        """前端构建产物位置：开发态 web/dist；打包后随资源目录。"""
        if getattr(sys, "frozen", False):
            meipass = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
            candidates = [
                meipass / "web",
                Path(sys.executable).resolve().parent / "web" / "dist",
            ]
        else:
            candidates = [PROJECT_ROOT / "web" / "dist"]
        for candidate in candidates:
            if (candidate / "index.html").is_file():
                return candidate
        return candidates[-1]

    @property
    def seed_dir(self) -> Path:
        """资产种子目录（holdexar_seed.db）：开发态 assets/seed；打包后随资源目录。"""
        if getattr(sys, "frozen", False):
            meipass = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
            candidates = [
                meipass / "seed",
                Path(sys.executable).resolve().parent / "seed",
            ]
        else:
            candidates = [PROJECT_ROOT / "assets" / "seed"]
        for candidate in candidates:
            if (candidate / f"{APP_SLUG}_seed.db").is_file():
                return candidate
        return candidates[0]

    @property
    def clash_dir(self) -> Path:
        """随包 Clash 内核目录（mihomo + GeoIP 数据 + 许可原文）。

        开发态 assets/clash；打包后随资源目录（spec datas 的 clash/）。
        内核与 GeoIP 数据是**随包资产**：内核版本固定在
        app.domains.proxies.kernel_release.MIHOMO_VERSION，用户机器上不再需要
        自装 Clash 或检索本机内核。

        判定与 seed_dir 同构：命中「目录里有内核可执行文件」才算数，否则回落
        首位候选（缺失时调用方负责走补资产/下载链，而不是在这里静默换地方）。
        """
        kernel_name = "mihomo.exe" if sys.platform == "win32" else "mihomo"
        if getattr(sys, "frozen", False):
            meipass = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
            candidates = [
                meipass / "clash",
                Path(sys.executable).resolve().parent / "clash",
            ]
        else:
            candidates = [PROJECT_ROOT / "assets" / "clash"]
        for candidate in candidates:
            if (candidate / kernel_name).is_file():
                return candidate
        return candidates[0]


@lru_cache
def get_settings() -> Settings:
    return Settings()
