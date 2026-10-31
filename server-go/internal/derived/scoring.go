// Package derived：价格派生维护链（hl_flag/pp_flag/pp_changed_at/
// min_cny_fen/diff_fen/smart_score 六列一次 UPDATE + series_id 系列聚类）。
// 全部在调用方事务连接上执行，写闸由调用方持有；闸内只做库读库写。
package derived

import (
	"math"
	"time"
)

// ── 智能评分四因子权重（调参改这里，不动公式）──
const (
	wSave        = 0.50
	wQuality     = 0.28
	wTiming      = 0.14
	wFamiliarity = 0.08

	// 熟悉度满分评测数
	familiarCapReviews = 100000

	// 时机四档（键 = hl_flag）：无折扣一律 0，史低+90%off 不重复奖励
	timingFlatLow  = 0.75
	timingNewLow   = 1.00
	timingDiscount = 0.35

	// 零评价成熟度曲线：quality = 0.2 + 0.3·exp(-age年/2)
	noReviewFloor    = 0.20
	noReviewTauYears = 2.0
)

// SaveScore 省钱因子：对数压缩的绝对差价（分），¥200 封顶 1.0。
func SaveScore(diffFen *int64) float64 {
	var d float64
	if diffFen != nil && *diffFen > 0 {
		d = float64(*diffFen) / 100.0
	}
	return math.Min(1.0, math.Log1p(d/20.0)/math.Log(11))
}

// releaseAgeYears 发行至今年数；缺失/不可解析/未来日期 = 0（按刚发行）。
func releaseAgeYears(releaseDate *string, now time.Time) float64 {
	if releaseDate == nil || *releaseDate == "" {
		return 0.0
	}
	head := *releaseDate
	if len(head) > 10 {
		head = head[:10]
	}
	day, err := time.Parse("2006-01-02", head)
	if err != nil {
		return 0.0
	}
	ny, nm, nd := now.Date()
	today := time.Date(ny, nm, nd, 0, 0, 0, 0, time.UTC)
	return math.Max(0.0, today.Sub(day).Hours()/24.0/365.25)
}

// QualityScore 质量因子：有评测走置信度收缩；零评价按发行成熟度从中性
// 0.5 滑向地板（有评测但好评率缺失 = 抓取缺口，中性）。
func QualityScore(positiveRate, reviewCount *int64, releaseDate *string, now time.Time) float64 {
	var n int64
	if reviewCount != nil {
		n = *reviewCount
	}
	if n > 0 {
		if positiveRate == nil {
			return 0.5
		}
		p := float64(*positiveRate) / 10000.0
		return p - (p-0.5)*math.Pow(2.0, -math.Log10(float64(n+1)))
	}
	age := releaseAgeYears(releaseDate, now)
	return noReviewFloor + (0.5-noReviewFloor)*math.Exp(-age/noReviewTauYears)
}

// TimingScore 购买时机因子：无折扣 0，有折扣按史低标记分档。
func TimingScore(hlFlag, discountPercent *int64) float64 {
	if discountPercent == nil || *discountPercent <= 0 {
		return 0.0
	}
	var flag int64
	if hlFlag != nil {
		flag = *hlFlag
	}
	switch flag {
	case 1:
		return timingNewLow
	case 2:
		return timingFlatLow
	}
	return timingDiscount
}

// FamiliarityScore 熟悉度因子：评测规模对数，10 万封顶（刻意轻权重）。
func FamiliarityScore(reviewCount *int64) float64 {
	var n int64
	if reviewCount != nil {
		n = *reviewCount
	}
	return math.Min(1.0, math.Log10(float64(n+1))/math.Log10(familiarCapReviews+1))
}

// SmartScore 综合分（0~1）：四因子加权，无隐藏项。
func SmartScore(diffFen, positiveRate, reviewCount, hlFlag, discountPercent *int64, releaseDate *string, now time.Time) float64 {
	return wSave*SaveScore(diffFen) +
		wQuality*QualityScore(positiveRate, reviewCount, releaseDate, now) +
		wTiming*TimingScore(hlFlag, discountPercent) +
		wFamiliarity*FamiliarityScore(reviewCount)
}
