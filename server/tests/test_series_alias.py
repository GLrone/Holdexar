"""系列识别的官定同系改写表：外传/姐妹作官方名不以系列词开头，
靠 _FORCED_SERIES_PREFIX 补系列词后并入正确簇（泛用起手不得误吞）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domains.games.series import (
    _FORCED_SERIES_PREFIX,
    _series_tokens,
    build_clusters,
    is_same_series,
)


def test_forced_prefix_rewrites_head_tokens():
    # 危机核心：系列词在分隔符后，改写后以 final fantasy 开头
    toks = _series_tokens("CRISIS CORE –FINAL FANTASY VII– REUNION", "")
    assert toks[:2] == ["final", "fantasy"]
    # 键按整 token 串前缀匹配：World of Warcraft 不得命中
    wow = _series_tokens("World of Warcraft", "")
    assert wow[:2] != ["final", "fantasy"]
    assert wow[0] == "world"
    # 姐妹作共享同一强制前缀
    a = _series_tokens("ENDER LILIES: Quietus of the Knights", "")
    b = _series_tokens("ENDER MAGNOLIA: Bloom in the Mist", "")
    assert a[:2] == b[:2] == ["ender", "series"]


def test_forced_pairs_rule_same_series():
    assert is_same_series(
        "危机核心 -最终幻想7- 重聚", "CRISIS CORE –FINAL FANTASY VII– REUNION",
        "FINAL FANTASY VII", None,
    )
    assert is_same_series(
        "终焉之莉莉：骑士寂夜", "ENDER LILIES: Quietus of the Knights",
        "终焉 玛格诺利亚:雾中之花", "ENDER MAGNOLIA: Bloom in the Mist",
    )


def test_clusters_collapse_with_forced_pairs():
    rows = [
        (1, "FINAL FANTASY VII REMAKE INTERGRADE", "FINAL FANTASY VII REMAKE INTERGRADE"),
        (2, "FINAL FANTASY XV WINDOWS EDITION", "FINAL FANTASY XV WINDOWS EDITION"),
        (3, "危机核心 -最终幻想7- 重聚", "CRISIS CORE –FINAL FANTASY VII– REUNION"),
        (4, "光之归来：最终幻想13", "LIGHTNING RETURNS: FINAL FANTASY XIII"),
        (5, "终焉之莉莉：骑士寂夜", "ENDER LILIES: Quietus of the Knights"),
        (6, "终焉 玛格诺利亚:雾中之花", "ENDER MAGNOLIA: Bloom in the Mist"),
        (7, "World of Warcraft", "World of Warcraft"),
    ]
    clusters = dict(build_clusters(rows))
    names_by_appid = {a: n for a, n, _ in rows}
    ff = [appid for appid, members in clusters.items() if appid in (1, 2, 3, 4)]
    # 1/2/3/4 同簇：找 1 所在簇的成员清单
    ff_cluster = next(m for m in clusters.values() if 1 in m)
    assert set(ff_cluster) == {1, 2, 3, 4}
    ender_cluster = next(m for m in clusters.values() if 5 in m)
    assert set(ender_cluster) == {5, 6}
    assert all(7 not in m or len(m) == 1 for m in clusters.values()) or 7 not in ff_cluster
