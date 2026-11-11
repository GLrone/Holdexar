package bills

import (
	"encoding/json"
	"fmt"
	"math"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"unicode"
	"unicode/utf8"
)

var (
	reportCurrencySymbols = sortedReportCurrencySymbols()
	reportMoneyDigits     = regexp.MustCompile(`^[\p{Nd}\s\p{Z}\v\x85,.\-]+`)
	reportCommaDash       = regexp.MustCompile(`,-+$`)
	reportMoneySpaces     = strings.NewReplacer(" ", "", "\u00a0", "", "\u202f", "")
	reportGroupSpaces     = strings.NewReplacer("\u00a0", " ", "\u202f", " ")
	reportChineseDate     = regexp.MustCompile(`(\p{Nd}{4})\s*年\s*(\p{Nd}{1,2})\s*月\s*(\p{Nd}{1,2})\s*日`)
	reportISODate         = regexp.MustCompile(`^\p{Nd}{4}-\p{Nd}{2}-\p{Nd}{2}`)
	reportEnglishDate     = regexp.MustCompile(`^(\p{Nd}{1,2})\s+([A-Za-z]{3}),?\s+(\p{Nd}{4})`)
)

type reportCurrencySymbol struct {
	symbol, code string
}

func sortedReportCurrencySymbols() []reportCurrencySymbol {
	symbols := []reportCurrencySymbol{
		{"CDN$", "CAD"}, {"MX$", "MXN"}, {"HK$", "HKD"}, {"NT$", "TWD"}, {"NZ$", "NZD"},
		{"A$", "AUD"}, {"S$", "SGD"}, {"R$", "BRL"}, {"RM", "MYR"}, {"Rp", "IDR"},
		{"CHF", "CHF"}, {"AED", "AED"}, {"QAR", "QAR"}, {"QR", "QAR"}, {"KD", "KWD"},
		{"SR", "SAR"}, {"lei", "RON"}, {"kr", "NOK"}, {"Ft", "HUF"}, {"Kč", "CZK"},
		{"zł", "PLN"}, {"฿", "THB"}, {"₫", "VND"}, {"₩", "KRW"}, {"€", "EUR"},
		{"£", "GBP"}, {"¥", "CNY"}, {"₪", "ILS"}, {"₡", "CRC"}, {"₸", "KZT"},
		{"₽", "RUB"}, {"₴", "UAH"}, {"₺", "TRY"}, {"₹", "INR"}, {"₱", "PHP"},
		{"CLP$", "CLP"}, {"COL$", "COP"}, {"$U", "UYU"}, {"S/.", "PEN"}, {"R ", "ZAR"},
		{"руб.", "RUB"}, {"руб", "RUB"}, {"TL", "TRY"}, {"$ USD", "USD"}, {"$", "USD"},
	}
	slices.SortStableFunc(symbols, func(a, b reportCurrencySymbol) int {
		return utf8.RuneCountInString(b.symbol) - utf8.RuneCountInString(a.symbol)
	})
	return symbols
}

func reportDigit(r rune) rune {
	if r >= '0' && r <= '9' {
		return r
	}
	for _, span := range unicode.Digit.R16 {
		if r >= rune(span.Lo) && r <= rune(span.Hi) {
			return '0' + (r-rune(span.Lo))/rune(span.Stride)%10
		}
	}
	for _, span := range unicode.Digit.R32 {
		if r >= rune(span.Lo) && r <= rune(span.Hi) {
			return '0' + (r-rune(span.Lo))/rune(span.Stride)%10
		}
	}
	return -1
}

func parseReportNumber(raw string) (float64, bool) {
	raw = reportCommaDash.ReplaceAllString(strings.TrimSpace(raw), ".00")
	raw = reportMoneySpaces.Replace(raw)
	if left, right, ok := strings.Cut(raw, ","); ok && !strings.Contains(raw, ".") &&
		!strings.Contains(right, ",") && utf8.RuneCountInString(right) <= 2 {
		raw = left + "." + right
	} else {
		raw = strings.ReplaceAll(raw, ",", "")
	}
	raw = strings.Map(func(r rune) rune {
		if r == '.' {
			return r
		}
		if unicode.IsDigit(r) {
			return reportDigit(r)
		}
		return -1
	}, raw)
	number, err := strconv.ParseFloat(raw, 64)
	return number, err == nil
}

func parseReportMoney(raw string) (float64, string) {
	text := reportGroupSpaces.Replace(raw)
	text = strings.TrimSpace(strings.TrimSuffix(strings.TrimSpace(text), "钱包资金"))
	sign := 1.0
	if strings.HasPrefix(text, "-") {
		sign = -1
	}
	text = strings.TrimSpace(strings.TrimLeft(text, "+-"))
	text = strings.TrimSpace(strings.TrimSuffix(text, "Credit"))
	leading := reportMoneyDigits.FindString(text)
	suffix := strings.TrimLeftFunc(text[len(leading):], unicode.IsSpace)
	for _, currency := range reportCurrencySymbols {
		var amount string
		if rest, ok := strings.CutPrefix(text, currency.symbol); ok {
			amount = reportMoneyDigits.FindString(rest)
		}
		if amount == "" && strings.HasPrefix(suffix, currency.symbol) {
			amount = leading
		}
		if amount != "" {
			if value, ok := parseReportNumber(amount); ok {
				return sign * value, currency.code
			}
		}
	}
	return 0, ""
}

func reportText(raw string) string {
	return strings.Join(strings.Fields(raw), " ")
}

func reportDateNumber(digits string) int {
	value, _ := strconv.Atoi(strings.Map(reportDigit, digits))
	return value
}

func parseDate(raw string) string {
	text := reportText(raw)
	if parts := reportChineseDate.FindStringSubmatch(text); parts != nil {
		return fmt.Sprintf("%s-%02d-%02d", parts[1], reportDateNumber(parts[2]), reportDateNumber(parts[3]))
	}
	if date := reportISODate.FindString(text); date != "" {
		return date
	}
	if parts := reportEnglishDate.FindStringSubmatch(text); parts != nil {
		months := []string{"jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"}
		if month := slices.Index(months, strings.ToLower(parts[2])); month >= 0 {
			return fmt.Sprintf("%d-%02d-%02d", reportDateNumber(parts[3]), month+1, reportDateNumber(parts[1]))
		}
	}
	return strings.TrimSpace(raw)
}

var reportHistoryTypes = []struct {
	pattern *regexp.Regexp
	name    string
}{
	{regexp.MustCompile(`(?i)退款|Refund`), "退款"},
	{regexp.MustCompile(`(?i)转换|轉換|Convert`), "转换"},
	{regexp.MustCompile(`(?i)游戏内(?:物品)?购买|遊戲內物品購買|In-Game\s*Purchase`), "游戏内购买"},
	{regexp.MustCompile(`(?i)市场交易|市集交易|Market\s*Transaction`), "市场交易"},
	{regexp.MustCompile(`(?i)礼物购买|禮物購買|Gift\s*Purchase`), "礼物购买"},
	{regexp.MustCompile(`(?i)购买|購買|Purchase`), "购买"},
}

var reportWalletPayment = regexp.MustCompile(`(?i)钱包资金|錢包資金|Wallet`)

func reportHistoryType(raw, item string) string {
	switch raw {
	case "Purchase", "购买":
		return "购买"
	case "Gift Purchase", "礼物购买":
		return "礼物购买"
	case "In-Game Purchase", "游戏内购买":
		return "游戏内购买"
	case "Refund", "退款":
		return "退款"
	case "Market Transaction":
		return "市场交易"
	case "Conversion":
		return "转换"
	}
	for _, kind := range reportHistoryTypes {
		if !kind.pattern.MatchString(raw) {
			continue
		}
		if kind.name == "市场交易" || kind.name == "转换" && !reportWalletPayment.MatchString(item) {
			break
		}
		if kind.name == "购买" && reportWalletPayment.MatchString(item) {
			return "转换"
		}
		return kind.name
	}
	return raw
}

func reportWalletItem(item string) bool {
	text := strings.ToLower(item)
	return strings.Contains(text, "钱包资金") ||
		strings.Contains(text, "钱包") && (strings.Contains(text, "资金") || strings.Contains(text, "wallet")) ||
		strings.Contains(text, "wallet credit") || strings.Contains(text, "wallet fund") ||
		strings.HasPrefix(text, "purchased") && strings.Contains(text, "credit")
}

func reportGameItems(item string, explicitRecipients []string) ([]string, []string) {
	text := strings.TrimSpace(strings.TrimSuffix(item, "退款"))
	parts := strings.Split(text, "礼物已发送给")
	var recipients []string
	for _, raw := range explicitRecipients {
		if name := reportText(raw); name != "" {
			recipients = append(recipients, name)
		}
	}
	if len(parts) > 1 {
		if len(recipients) == 0 {
			recipients = parts[1:]
			for i, name := range recipients {
				recipients[i] = strings.TrimSpace(name)
			}
		}
		text = strings.Trim(parts[0], " ,、")
	}
	items := make([]string, 0)
	for _, part := range strings.FieldsFunc(text, func(r rune) bool { return r == ',' || r == '，' }) {
		if title := strings.TrimSpace(part); title != "" {
			items = append(items, title)
		}
	}
	if len(items) == 0 && text != "" {
		items = []string{text}
	}
	return items, recipients
}

func reportOriginalPrice(raw string) *float64 {
	if value, currency := parseReportMoney(raw); currency != "" {
		value = math.Abs(value)
		return &value
	}
	if value, ok := parseReportNumber(raw); ok {
		return &value
	}
	return nil
}

func reportPaymentParts(raw json.RawMessage) json.RawMessage {
	var value any
	if len(raw) == 0 || json.Unmarshal(raw, &value) != nil {
		return nil
	}
	switch v := value.(type) {
	case []any:
		if len(v) != 0 {
			return raw
		}
	case map[string]any:
		if len(v) != 0 {
			return raw
		}
	case string:
		if v != "" {
			return raw
		}
	case float64:
		if v != 0 {
			return raw
		}
	case bool:
		if v {
			return raw
		}
	}
	return nil
}

type reportMatchKey struct {
	amount, items string
}

func gameReportKey(tx gameTx) reportMatchKey {
	// Historical matching uses the rounded amount and title, regardless of currency.
	return reportMatchKey{
		amount: strconv.FormatFloat(tx.Amount, 'f', 4, 64),
		items:  strings.ToLower(reportText(strings.Join(tx.Items, " | "))),
	}
}

func dedupeReportGames(candidates []gameTx) []gameTx {
	type dateKey struct {
		date string
		reportMatchKey
	}
	type group struct{ purchase, refund *gameTx }
	grouped := make(map[dateKey]group)
	for i := range candidates {
		tx := &candidates[i]
		key := dateKey{tx.Date, gameReportKey(*tx)}
		g := grouped[key]
		if tx.Refund {
			if g.refund == nil {
				g.refund = tx
			}
		} else if g.purchase == nil || tx.Gift && !g.purchase.Gift {
			g.purchase = tx
		}
		grouped[key] = g
	}
	var chosen []gameTx
	for _, g := range grouped {
		if g.purchase != nil {
			chosen = append(chosen, *g.purchase)
		}
		if g.refund != nil {
			g.refund.OrigGift = g.purchase != nil && g.purchase.Gift
			chosen = append(chosen, *g.refund)
		}
	}
	slices.SortFunc(chosen, func(a, b gameTx) int {
		if dateOrder := strings.Compare(a.Date, b.Date); dateOrder != 0 {
			return dateOrder
		}
		return a.RawIdx - b.RawIdx
	})
	purchases := make(map[reportMatchKey][]*gameTx)
	for i := range chosen {
		tx := &chosen[i]
		if !tx.Refund {
			key := gameReportKey(*tx)
			purchases[key] = append(purchases[key], tx)
		}
	}
	for i := range chosen {
		tx := &chosen[i]
		if !tx.Refund {
			continue
		}
		key := gameReportKey(*tx)
		bucket := purchases[key]
		if len(bucket) == 0 {
			continue
		}
		match := len(bucket) - 1
		for n := match; n >= 0; n-- {
			if bucket[n].Date <= tx.Date {
				match = n
				break
			}
		}
		tx.OrigGift = bucket[match].Gift
		purchases[key] = slices.Delete(bucket, match, match+1)
	}
	slices.Reverse(chosen)
	return chosen
}

var reportLicenseKinds = []struct {
	pattern    *regexp.Regexp
	acq, label string
}{
	{regexp.MustCompile(`(?i)Steam\s*商店|Steam\s*Store`), "", ""},
	{regexp.MustCompile(`(?i)零售|Retail`), "cdk", "零售"},
	{regexp.MustCompile(`(?i)免费赠送|贈品|Complimentary`), "free", "免费赠送"},
	{regexp.MustCompile(`(?i)礼物|玩家通行证|禮物|招待券|Gift|Guest\s*Pass`), "gift", "礼物/玩家通行证"},
}

func parseReport(report steamReport) parsedReport {
	out := parsedReport{Nickname: reportText(report.Nickname), Avatar: report.Avatar}
	var candidates []gameTx
	for index, row := range report.History {
		item := reportText(row.Item)
		kind := reportHistoryType(reportText(row.Type), item)
		if kind == "转换" || strings.Contains(kind, "市场交易") {
			continue
		}
		date := parseDate(reportText(row.Date))
		payment := reportText(row.Payment)
		amount, currency := parseReportMoney(reportText(row.Total))
		amount = math.Abs(amount)
		basePrice := reportText(row.BasePrice)
		refund := kind == "退款"
		if (refund || kind == "购买") && reportWalletItem(item) || kind == "" && amount > 0 {
			if currency == "" {
				amount, currency = parseReportMoney(reportText(row.WalletChange))
				if currency == "" {
					amount, currency = parseReportMoney(basePrice)
				}
				amount = math.Abs(amount)
			}
			if amount > 0 {
				topup := topupTx{Date: date, Desc: item, Currency: currency, Payment: payment, Type: "钱包充值", Amount: amount, Sign: 1, Refund: refund}
				if topup.Desc == "" {
					topup.Desc = topup.Type
				}
				if refund {
					topup.Type, topup.Sign = "充值退款", -1
				}
				out.Topup = append(out.Topup, topup)
				continue
			}
		}
		if amount <= 0 {
			continue
		}
		switch kind {
		case "购买", "礼物购买", "游戏内购买", "退款":
		default:
			continue
		}
		items, recipients := reportGameItems(item, row.GiftRecipients)
		tx := gameTx{
			Date: date, Type: kind, Currency: currency, Payment: payment, Discount: row.Discount,
			WalletBalance: reportText(row.WalletBalance), BasePrice: basePrice, Items: items, GiftTo: recipients,
			PaymentParts: reportPaymentParts(row.PaymentParts), Amount: amount, OrigPrice: reportOriginalPrice(row.OriginalPrice),
			Sign: 1, RawIdx: index, Gift: kind == "礼物购买", Refund: refund,
		}
		if refund {
			tx.Sign = -1
		}
		candidates = append(candidates, tx)
	}
	out.Game = dedupeReportGames(candidates)
	for _, tx := range out.Game {
		if tx.Gift && len(tx.Items) > 1 {
			out.Warnings = append(out.Warnings, fmt.Sprintf("混合订单（%s · %s等）无法拆分单价，已按全额赠礼计算", tx.Date, tx.Items[0]))
		}
	}
	slices.SortStableFunc(out.Topup, func(a, b topupTx) int { return strings.Compare(b.Date, a.Date) })
	for _, row := range report.Licenses {
		method := reportText(row.Method)
		for _, kind := range reportLicenseKinds {
			if !kind.pattern.MatchString(method) {
				continue
			}
			if kind.acq != "" {
				appID := row.AppID
				if appID != nil && *appID == 0 {
					appID = nil
				}
				out.Licenses = append(out.Licenses, licenseTx{Name: reportText(row.Item), Date: parseDate(row.Date), Acq: kind.acq, AcqLabel: kind.label, AppID: appID})
			}
			break
		}
	}
	return out
}
