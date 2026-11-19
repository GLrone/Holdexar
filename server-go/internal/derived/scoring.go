package derived

import (
	"math"
	"time"
)

const (
	wSave        = 0.50
	wQuality     = 0.28
	wTiming      = 0.14
	wFamiliarity = 0.08

	familiarCapReviews = 100000

	// 时机四档（键 = hl_flag）：无折扣一律 0，史低+90%off 不重复奖励
	timingFlatLow  = 0.75
	timingNewLow   = 1.00
	timingDiscount = 0.35

	noReviewFloor    = 0.20
	noReviewTauYears = 2.0
)

func SaveScore(diffFen *int64) float64 {
	var d float64
	if diffFen != nil && *diffFen > 0 {
		d = float64(*diffFen) / 100.0
	}
	return math.Min(1.0, math.Log1p(d/20.0)/math.Log(11))
}

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

// QualityScore 有评测做置信度收缩；零评测随发行年数滑向地板，有评测缺好评率取中性 0.5。
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

func FamiliarityScore(reviewCount *int64) float64 {
	var n int64
	if reviewCount != nil {
		n = *reviewCount
	}
	return math.Min(1.0, math.Log10(float64(n+1))/math.Log10(familiarCapReviews+1))
}

func SmartScore(diffFen, positiveRate, reviewCount, hlFlag, discountPercent *int64, releaseDate *string, now time.Time) float64 {
	return BlendScore(SaveScore(diffFen), QualityScore(positiveRate, reviewCount, releaseDate, now), TimingScore(hlFlag, discountPercent), FamiliarityScore(reviewCount))
}

func BlendScore(save, quality, timing, familiarity float64) float64 {
	return wSave*save + wQuality*quality + wTiming*timing + wFamiliarity*familiarity
}
