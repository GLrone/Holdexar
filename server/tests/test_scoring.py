"""smart 排序评分（scoring.py）的公式验收。

数值断言对齐评分对照表（对数压缩 / SteamDB 收缩 / 零评价成熟度 / timing 四档 /
熟悉度封顶），调权重或常量时应同步这里的期望值。
"""
from datetime import date, timedelta

import pytest

from app.domains.games.scoring import (
    NO_REVIEW_FLOOR,
    NO_REVIEW_TAU_YEARS,
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


def _days_ago(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


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
    """S_quality：有评测走 SteamDB 收缩；零评测按发行成熟度从中性滑向地板。"""

    def test_no_release_info_neutral(self):
        # 无发行日期信息无从判断成熟度，维持中性（不惩罚数据缺口）
        assert quality_score(None, 0) == 0.5
        assert quality_score(9800, 0) == 0.5  # 有好评率但无评测 = 无数据
        assert quality_score(9800, 0, None) == 0.5
        assert quality_score(9800, 0, "not-a-date") == 0.5

    def test_zero_reviews_just_released_near_neutral(self):
        # 刚发行一周：评价尚未形成，接近中性不被惩罚
        q = quality_score(None, 0, _days_ago(7))
        assert 0.49 < q <= 0.5

    def test_zero_reviews_old_years_below_neutral(self):
        # 发行 3 年仍 0 评价：明显低于中性（≈0.27），不能继续享受完整 0.5
        q = quality_score(None, 0, _days_ago(3 * 365))
        assert NO_REVIEW_FLOOR < q < 0.32

    def test_zero_reviews_decade_old_at_floor(self):
        # 发行 10 年仍 0 评价：贴着地板但不到达（渐近，不是绝对负面归零）
        q = quality_score(None, 0, _days_ago(10 * 365))
        assert NO_REVIEW_FLOOR < q < NO_REVIEW_FLOOR + 0.02

    def test_zero_reviews_monotonic_no_jump(self):
        # 随发行年龄单调下降、平滑无阈值跳变
        qs = [quality_score(None, 0, _days_ago(d)) for d in (30, 90, 180, 365, 730, 1095, 1825, 3650)]
        assert all(a > b for a, b in zip(qs, qs[1:]))

    def test_future_release_counts_as_new(self):
        # 未发售（未来发行日期）按刚发行 = 中性
        future = (date.today() + timedelta(days=30)).isoformat()
        assert quality_score(None, 0, future) == 0.5

    def test_few_reviews_shrunk(self):
        # 100% 好评 / 3 条 ≠ 满分：向 0.5 收缩
        assert quality_score(10000, 3) == pytest.approx(0.6706, abs=1e-3)

    def test_many_reviews_trusted(self):
        # 96% 好评 / 20 万条 ≈ 0.948（接近原始 0.96）
        assert quality_score(9600, 200000) == pytest.approx(0.9483, abs=1e-3)

    def test_negative_rating_below_neutral(self):
        assert quality_score(4000, 50000) < 0.5

    def test_tau_constant_controls_decay(self):
        # τ 时间常数语义：2 年零评价走过中性→地板的 63%
        q2y = quality_score(None, 0, _days_ago(2 * 365))
        assert q2y == pytest.approx(
            NO_REVIEW_FLOOR + (0.5 - NO_REVIEW_FLOOR) * pow(2.718281828, -1.0), abs=0.01
        )


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

    def test_old_zero_review_loses_to_reviewed_rival(self):
        """老游戏零评价+差价+新史低：不能仅靠 save+timing 压过同档真实评测。

        对照组同为新史低、差价 ¥20 vs ¥30（save 差 < 质量惩罚）：发行 3 年
        仍零评价的款质量跌破中性后总分落到有评测款之下；刚发行的零评价款
        不受同等压制（新游戏暂无评价是合法状态）。
        """
        d3y = _days_ago(3 * 365)
        suspect = smart_score(3000, None, 0, 1, 30, d3y)  # 3 年 0 评价 + ¥30 差 + 新史低
        rival = smart_score(2000, 6000, 15, 1, 30)  # ¥20 差 + 新史低 + 60%/15 条评测
        fresh = smart_score(3000, None, 0, 1, 30, _days_ago(7))
        assert suspect < rival
        assert fresh > suspect
