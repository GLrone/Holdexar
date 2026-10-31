package games

import (
	"math"
	"strconv"
	"strings"
)

// regionToCurrency 区码大写 → 币种；价格矩阵只收本表内的区（与 CC_LIST 口径一致）。
var regionToCurrency = map[string]string{
	"CN": "CNY", "RU": "RUB", "KZ": "KZT", "UA": "UAH", "PK": "USD",
	"TR": "USD", "AR": "USD", "AZ": "USD", "VN": "VND", "ID": "IDR",
	"IN": "INR", "BR": "BRL", "CL": "CLP", "JP": "JPY", "HK": "HKD",
	"PH": "PHP", "TW": "TWD", "KW": "KWD", "SA": "SAR", "ZA": "ZAR",
	"QA": "QAR", "MY": "MYR", "TH": "THB", "PE": "PEN", "MX": "MXN",
	"SG": "SGD", "AE": "AED", "UY": "UYU", "CO": "COP", "KR": "KRW",
	"NZ": "NZD", "PL": "PLN", "CR": "CRC", "CA": "CAD", "US": "USD",
	"AU": "AUD", "PT": "EUR", "GB": "GBP", "NO": "NOK", "IL": "ILS",
	"CH": "CHF",
}

// displayDecimals 各币种显示小数位（存储统一 2 位小数 cents）；缺省 2。
var displayDecimals = map[string]int{
	"CLP": 0, "VND": 0, "IDR": 0, "JPY": 0, "KZT": 0, "UAH": 0,
	"CNY": 2, "USD": 2, "RUB": 2, "INR": 2, "BRL": 2, "HKD": 2,
	"PHP": 2, "TWD": 2,
}

var currencySymbols = map[string]string{
	"CNY": "¥", "RUB": "₽", "KZT": "₸", "UAH": "₴",
	"USD": "$", "VND": "₫", "IDR": "Rp", "INR": "₹",
	"BRL": "R$", "CLP": "$", "JPY": "¥", "HKD": "HK$", "PHP": "₱",
	"TWD": "NT$", "KWD": "KD", "SAR": "SR", "ZAR": "R", "QAR": "QR",
	"MYR": "RM", "THB": "฿", "PEN": "S/", "MXN": "MX$", "SGD": "S$",
	"AED": "AED", "UYU": "$U", "COP": "COL$", "KRW": "₩", "NZD": "NZ$",
	"PLN": "zł", "CRC": "₡", "CAD": "C$", "AUD": "A$", "EUR": "€",
	"GBP": "£", "NOK": "kr", "ILS": "₪", "CHF": "CHF",
}

// suffixCurrencies 符号后置的币种（"55.00 ₽"）。
var suffixCurrencies = map[string]struct{}{
	"RUB": {}, "KZT": {}, "UAH": {}, "PLN": {}, "NOK": {},
}

// defaultExchangeRates 汇率兜底表（fx_rates 无对应行时使用）。
var defaultExchangeRates = map[string]float64{
	"CNY": 1.0, "USD": 7.25, "RUB": 0.079, "KZT": 0.015,
	"UAH": 0.175, "TRY": 0.22, "ARS": 0.0083, "VND": 0.00029,
	"IDR": 0.00046, "INR": 0.087, "BRL": 1.42, "CLP": 0.0076,
	"JPY": 0.048, "HKD": 0.928, "PHP": 0.126, "AZN": 4.26,
	"TWD": 0.21, "KWD": 3.31, "SAR": 1.93, "ZAR": 0.38,
	"QAR": 1.97, "MYR": 1.55, "THB": 0.20, "PEN": 2.12,
	"MXN": 0.42, "SGD": 5.40, "AED": 1.97, "UYU": 0.21,
	"COP": 0.0018, "KRW": 0.0052, "NZD": 4.50, "PLN": 2.15,
	"CRC": 0.13, "CAD": 5.20, "AUD": 4.70, "EUR": 7.90,
	"GBP": 9.20, "NOK": 0.66, "ILS": 1.95, "CHF": 8.10,
}

// formatMinorUnits cents → 本币字符串（"¥42.00" / "608" 前置符号 / "55.00 ₽" 后置符号）。
func formatMinorUnits(minor int64, currency string) string {
	c := strings.ToUpper(currency)
	symbol := currencySymbols[c]
	decimals, ok := displayDecimals[c]
	if !ok {
		decimals = 2
	}
	major := float64(minor) / 100
	sign := ""
	if major < 0 {
		sign = "-"
		major = -major
	}
	var formatted string
	if decimals == 0 {
		formatted = groupDigits(int64(math.RoundToEven(major)))
	} else {
		formatted = groupFloat(strconv.FormatFloat(major, 'f', decimals, 64))
	}
	if _, ok := suffixCurrencies[c]; ok {
		if symbol == "" {
			return sign + formatted
		}
		return sign + formatted + " " + symbol
	}
	return sign + symbol + formatted
}

// groupDigits 整数千分位逗号分组。
func groupDigits(n int64) string {
	s := strconv.FormatInt(n, 10)
	return groupCommas(s)
}

// groupFloat 对 "1234.56" 的整数部分做千分位分组。
func groupFloat(s string) string {
	dot := strings.IndexByte(s, '.')
	if dot < 0 {
		return groupCommas(s)
	}
	return groupCommas(s[:dot]) + s[dot:]
}

func groupCommas(s string) string {
	n := len(s)
	if n <= 3 {
		return s
	}
	var b strings.Builder
	pre := n % 3
	if pre > 0 {
		b.WriteString(s[:pre])
		if n > pre {
			b.WriteByte(',')
		}
	}
	for i := pre; i < n; i += 3 {
		b.WriteString(s[i : i+3])
		if i+3 < n {
			b.WriteByte(',')
		}
	}
	return b.String()
}

// convertMinorToCnyFen 按汇率折算 CNY 分；币种无汇率返回 nil。
func convertMinorToCnyFen(amountMinor int64, currency string, rates map[string]float64) *int64 {
	rate, ok := rates[strings.ToUpper(currency)]
	if !ok {
		return nil
	}
	v := int64(math.RoundToEven(float64(amountMinor) * rate))
	return &v
}

// roundN 四舍六入五成双保留 n 位小数（Python round 口径）。
func roundN(x float64, n int) float64 {
	p := math.Pow10(n)
	return math.RoundToEven(x*p) / p
}
