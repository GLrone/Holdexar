"""UI 收口 · 旗帜素材完整性验收（通用约定的机械保障）。

地区/币种旗帜由前端本地素材承载（flagUrl() → /flags/{code}.png），
RegionFlag 的 @error 会把缺失素材静默隐藏——线上"悄悄没旗"不可见。
此测试把"加区忘下载素材"提前到提交期报错：

1. CC_LIST 每个区码必须有 web/public/flags/{code}.png；
2. 前端币种表（web/src/api/currencies.ts）每个 cc 同样必须有素材
   （EUR 用 eu.png，预留币种 TRY/ARS 也须有图）；
3. flags 目录不允许孤儿文件（区服已删但素材残留，误引流）。

新增区服流程：server/app/crawler/config.py CC_LIST 加行
→ 下载 https://flagcdn.com/w40/{code}.png 到 web/public/flags/
→ 补 web/src/api/currencies.ts 币种行（如有新货币）。
"""
import re
from pathlib import Path

from app.crawler.config import CC_LIST

REPO_ROOT = Path(__file__).resolve().parents[2]
FLAGS_DIR = REPO_ROOT / "web" / "public" / "flags"
CURRENCIES_TS = REPO_ROOT / "web" / "src" / "api" / "currencies.ts"


def _currency_ccs() -> set[str]:
    """从前端币种表提取 flag 所属区码（cc 字段）。"""
    text = CURRENCIES_TS.read_text(encoding="utf-8")
    return {m.lower() for m in re.findall(r"cc:\s*'([A-Za-z]{2})'", text)}


def test_region_flags_complete():
    """CC_LIST 每个区码都必须有本地旗帜素材。"""
    missing = [code for code, _, _ in CC_LIST if not (FLAGS_DIR / f"{code}.png").is_file()]
    assert not missing, (
        f"区服缺旗帜素材: {missing}。"
        f"请下载 https://flagcdn.com/w40/{{code}}.png 到 web/public/flags/ "
        f"（否则前端 RegionFlag 会静默隐藏旗帜，违反通用约定）"
    )


def test_currency_cc_flags_complete():
    """币种表 cc（含预留币种与 eu 合成旗）都必须有素材。"""
    missing = sorted(cc for cc in _currency_ccs() if not (FLAGS_DIR / f"{cc}.png").is_file())
    assert not missing, (
        f"币种表（web/src/api/currencies.ts）cc 缺旗帜素材: {missing}。"
        f"欧元区素材为 eu.png；请补齐或修正 cc 指向"
    )


def test_flags_no_orphans():
    """flags 目录不允许孤儿素材（区服/币种都不引用的文件）。"""
    # bd = 捆绑包南亚容灾区（孟加拉，refresh BD 落行/前端 BD 展示用）
    required = {code for code, _, _ in CC_LIST} | _currency_ccs() | {"bd"}
    orphans = sorted(
        p.stem for p in FLAGS_DIR.glob("*.png") if p.stem.lower() not in required
    )
    assert not orphans, (
        f"flags/ 存在孤儿素材: {orphans}。"
        f"区服/币种均已不引用，请删除避免误导后续加区"
    )
