"""smart 排序评分（scoring.py）的公式验收。

数值断言对齐评分对照表（对数压缩 / SteamDB 收缩 / timing 四档 / 熟悉度封顶），
调权重或常量时应同步这里的期望值。
"""
import pytest

from app.domains.games.scoring import (
    familiarity_score,
    quality_score,
    save_score,
    smart_score,
    timing_score,
    W_SAVE,
    W_QUALITY,
    W_TIMING,
    W_FAMILIARITY,
)


class TestSaveScore:
    """S_save = min(1, ln(1+D/20)/ln(11))：¥0→0，¥200 封顶 1.0。"""

    @pytest.mark.parametrize(
        ("yuan", "expected"),
        [(0, 0.0), (10, 0.169), (20, 0.289), (50, 0.522), (100, 0.747), (200, 1.0)],
    )
    def test_table(self, yuan, expected):
        assert save_score(yuan * 100) == pytest.approx(expected, abs=0.002)

    def test_cap_over_200(self):
        assert save_score(500 * 100) == 1.0

    def test_none_and_negative(self):
        assert save_score(None) == 0.0
        assert save_score(-500) == 0.0


class TestQualityScore:
    """S_quality：零评测中性 0.5；少量评测向 0.5 收缩；差评可低于 0.5。"""

    def test_zero_reviews_neutral(self):
        assert quality_score(None, 0) == 0.5
        assert quality_score(9800, 0) == 0.5  # 有好评率但无评测 = 无数据

    def test_few_reviews_shrunk(self):
        # 100% 好评 / 3 条 ≠ 满分：向 0.5 收缩
        assert quality_score(10000, 3) == pytest.approx(0.6706, abs=1e-3)

    def test_many_reviews_trusted(self):
        # 96% 好评 / 20 万条 ≈ 0.948（接近原始 0.96）
        assert quality_score(9600, 200000) == pytest.approx(0.9483, abs=1e-3)

    def test_negative_rating_below_neutral(self):
        assert quality_score(4000, 50000) < 0.5


class TestTimingScore:
    """S_timing 四档；无折扣一律 0（hl 标记不脱离折扣独立生效）。"""

    def test_full_price(self):
        assert timing_score(0, 0) == 0.0
        assert timing_score(2, 0) == 0.0  # 平史低标记但当前无折扣

    def test_discount_tiers(self):
        assert timing_score(0, 30) == 0.35  # 普通折扣（无 hl 标记）
        assert timing_score(3, 50) == 0.35  # 打折非史低
        assert timing_score(2, 50) == 0.75  # 平史低
        assert timing_score(1, 50) == 1.00  # 新史低

    def test_none_inputs(self):
        assert timing_score(None, None) == 0.0


class TestFamiliarityScore:
    """S_familiarity：log10(N+1)/log10(100001)，10 万封顶。"""

    def test_scale(self):
        assert familiarity_score(0) == 0.0
        assert familiarity_score(1000) == pytest.approx(0.600, abs=1e-3)
        assert familiarity_score(100000) == 1.0
        assert familiarity_score(10**7) == 1.0  # 封顶

    def test_none(self):
        assert familiarity_score(None) == 0.0


class TestSmartScore:
    """加权和：因子各自正确时总和 = Σ w_i·S_i。"""

    def test_weights_sum_to_one(self):
        assert W_SAVE + W_QUALITY + W_TIMING + W_FAMILIARITY == pytest.approx(1.0)

    def test_total_is_weighted_sum(self):
        total = smart_score(5000, 9500, 5000, 1, 50)
        expected = (
            W_SAVE * save_score(5000)
            + W_QUALITY * quality_score(9500, 5000)
            + W_TIMING * timing_score(1, 50)
            + W_FAMILIARITY * familiarity_score(5000)
        )
        assert total == pytest.approx(expected, abs=1e-9)

    def test_product_example_ordering(self):
        """排序示例：C（高差价+新史低）> A（原价大作）≈ B（冷门新史低）。

        A 不被折扣硬墙压死（硬前缀字典序下 A 沉底），B 仍能凭时机进入同档——
        smart 排序要保的两个平衡。
        """
        c = smart_score(8000, 9500, 5000, 1, 50)
        a = smart_score(3000, 9700, 200000, 0, 0)
        b = smart_score(1500, 9000, 300, 1, 70)
        assert c > a > 0
        assert abs(a - b) < 0.05  # 同档竞争，而不是一边倒
