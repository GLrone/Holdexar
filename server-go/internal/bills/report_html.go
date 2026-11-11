package bills

import (
	"errors"
	"iter"
	"net/url"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"unicode"

	"golang.org/x/net/html"
	"golang.org/x/net/html/atom"
)

var (
	historyMarketCount = regexp.MustCompile(`(?i)^([0-9]+)[\p{Z}\s\x0b\x85]+(?:市场交易|Market Transactions?)$`)
	reportAvatarURL    = regexp.MustCompile(`^https://avatars\.[a-z.]+/.+`)
	billHTMLWhitespace = regexp.MustCompile(`[\p{Z}\s\x0b\x85]{2,}|[\t\n\r]`)
)

func parseHistoryHTML(raw string, firstPage bool) ([]historyRow, error) {
	var table *html.Node
	if firstPage {
		doc, err := html.Parse(strings.NewReader(raw))
		if err != nil {
			return nil, err
		}
		table = firstBillElement(doc, func(n *html.Node) bool {
			return n.DataAtom == atom.Table && billHasClass(n, "wallet_history_table")
		})
		if table == nil {
			return nil, errors.New("Steam 消费明细页缺少交易表格")
		}
	} else {
		// A document parse discards bare table rows returned by the AJAX endpoint.
		table = &html.Node{Type: html.ElementNode, DataAtom: atom.Table, Data: "table"}
		nodes, err := html.ParseFragment(strings.NewReader(raw), table)
		if err != nil {
			return nil, err
		}
		for _, node := range nodes {
			table.AppendChild(node)
		}
	}

	rows := []historyRow{}
	for tr := range billTableRows(table) {
		if !billHasClass(tr, "wallet_table_row") {
			continue
		}
		row := historyHTMLRow(tr)
		if row.Date == "" || row.Type == "" {
			return nil, errors.New("Steam 消费明细缺少交易日期或类型")
		}
		rows = append(rows, row)
	}
	return rows, nil
}

func historyHTMLRow(tr *html.Node) historyRow {
	cells := make(map[string]*html.Node)
	for td := range tr.ChildNodes() {
		if td.DataAtom != atom.Td {
			continue
		}
		for _, class := range strings.Fields(billAttribute(td, "class")) {
			if cells[class] == nil {
				cells[class] = td
			}
		}
	}
	date := billText(cells["wht_date"], nil)
	if date == "" {
		return historyRow{}
	}
	row := historyRow{
		Date: date, TypeCount: 1,
		WalletChange:  billText(cells["wht_wallet_change"], nil),
		WalletBalance: billText(cells["wht_wallet_balance"], nil),
		BasePrice:     billText(cells["wht_base_price"], nil),
	}
	if cell := cells["wht_items"]; cell != nil {
		row.Item, row.GiftRecipients = historyHTMLItems(cell)
	}
	if cell := cells["wht_type"]; cell != nil {
		var payment []string
		for i, block := range billTopDivs(cell) {
			if i == 0 {
				row.Type = billText(block, billHistoryAnnotation)
			} else if !billHistoryAnnotation(block) {
				if part := billText(block, billHistoryAnnotation); part != "" {
					payment = append(payment, part)
				}
			}
		}
		if row.Type == "" {
			row.Type = billText(cell, billHistoryAnnotation)
		}
		row.Payment = strings.Join(payment, " + ")
		if match := historyMarketCount.FindStringSubmatch(row.Type); match != nil {
			if count, err := strconv.Atoi(match[1]); err == nil && count > 0 {
				row.TypeCount = count
			}
		}
	}

	total := cells["wht_total"]
	row.OriginalPrice = billClassText(tr, "wht_original_price")
	if row.OriginalPrice == "" {
		row.OriginalPrice = billText(firstBillElement(total, billStrikePrice), nil)
	}
	row.Discount = billClassText(tr, "wht_discount_pct")
	if row.Discount == "" {
		row.Discount = billClassText(tr, "wht_discount")
	}
	row.Total = billText(total, func(n *html.Node) bool {
		return billStrikePrice(n) || billHasClass(n, "wht_original_price") || billHasClass(n, "wht_discount_pct") || billHasClass(n, "wht_discount")
	})
	return row
}

func historyHTMLItems(cell *html.Node) (string, []string) {
	var items, recipients []string
	for _, block := range billTopDivs(cell) {
		if name := billText(block, billHistoryAnnotation); name != "" && !slices.Contains(items, name) {
			items = append(items, name)
		}
	}
	for n := range cell.Descendants() {
		if n.DataAtom != atom.A || !slices.ContainsFunc(n.Attr, func(a html.Attribute) bool { return a.Key == "data-miniprofile" }) {
			continue
		}
		if name := billText(n, nil); name != "" && !slices.Contains(recipients, name) {
			recipients = append(recipients, name)
		}
	}
	if len(items) == 0 {
		return billText(cell, billHistoryAnnotation), recipients
	}
	return strings.Join(items, ", "), recipients
}

func parseLicenseHTML(raw string) ([]licenseRow, string, error) {
	doc, err := html.Parse(strings.NewReader(raw))
	if err != nil {
		return nil, "", err
	}
	nextLink := firstBillElement(doc, func(n *html.Node) bool {
		return n.DataAtom == atom.A && billHasClass(n, "license_paginator_next") && billAttribute(n, "href") != ""
	})
	next := billAttribute(nextLink, "href")
	var fallback []licenseRow
	for n := range doc.Descendants() {
		if n.DataAtom != atom.Table {
			continue
		}
		rows, malformed, hasEmptyMarker := licenseHTMLRows(n)
		if billHasClass(n, "account_table") {
			if malformed {
				return nil, "", errors.New("Steam 游戏许可记录内容不完整")
			}
			return rows, next, nil
		}
		if fallback == nil && (len(rows) > 0 || hasEmptyMarker) {
			if malformed {
				return nil, "", errors.New("Steam 游戏许可记录内容不完整")
			}
			fallback = rows
		}
	}
	if fallback != nil {
		return fallback, next, nil
	}
	return nil, "", errors.New("Steam 游戏许可页缺少记录表格")
}

func licenseHTMLRows(table *html.Node) ([]licenseRow, bool, bool) {
	rows := []licenseRow{}
	var malformed, hasEmptyMarker bool
	for tr := range billTableRows(table) {
		var date, item, method *html.Node
		var header bool
		for td := range tr.ChildNodes() {
			if td.DataAtom == atom.Th {
				header = true
				break
			}
			if td.DataAtom != atom.Td {
				continue
			}
			switch {
			case billHasClass(td, "license_date_col"):
				date = td
			case billHasClass(td, "license_acquisition_col"):
				method = td
			case item == nil && !billHasClass(td, "steamdb_license_id_col"):
				item = td
			}
		}
		if header {
			continue
		}
		name := billText(item, billLicenseRemoveLink)
		if strings.Contains(name, "未找到许可") || strings.Contains(name, "找不到許可") || strings.Contains(name, "No licenses found") || strings.Contains(name, "No license found") {
			hasEmptyMarker = true
			continue
		}
		rawDate := billText(date, nil)
		if rawDate != "" && strings.IndexFunc(rawDate, unicode.IsDigit) < 0 {
			continue
		}
		if rawDate != "" && name != "" && method != nil {
			rows = append(rows, licenseRow{Date: rawDate, Item: name, Method: billText(method, nil), AppID: billLicenseAppID(item)})
		} else if date != nil || method != nil {
			malformed = true
		}
	}
	return rows, malformed, hasEmptyMarker
}

func billLicenseAppID(item *html.Node) *int64 {
	for n := range item.Descendants() {
		if n.DataAtom != atom.A || billLicenseRemoveLink(n) {
			continue
		}
		link, err := url.Parse(billAttribute(n, "href"))
		if err != nil || (link.Host != "" && !strings.EqualFold(link.Hostname(), "store.steampowered.com")) {
			continue
		}
		path, ok := strings.CutPrefix(link.Path, "/app/")
		if !ok {
			continue
		}
		id, _, _ := strings.Cut(path, "/")
		if appID, err := strconv.ParseUint(id, 10, 63); err == nil && appID > 0 {
			value := int64(appID)
			return &value
		}
	}
	return nil
}

func parseIdentityHTML(raw string) (nickname, avatarURL string, err error) {
	doc, err := html.Parse(strings.NewReader(raw))
	if err != nil {
		return "", "", err
	}
	account := firstBillElement(doc, func(n *html.Node) bool { return billAttribute(n, "id") == "account_pulldown" })
	avatar := firstBillElement(doc, func(n *html.Node) bool {
		return n.DataAtom == atom.Img && reportAvatarURL.MatchString(billAttribute(n, "src"))
	})
	return billText(account, nil), billAttribute(avatar, "src"), nil
}

func billAttribute(n *html.Node, key string) string {
	if n != nil {
		for _, attr := range n.Attr {
			if attr.Key == key && attr.Namespace == "" {
				return attr.Val
			}
		}
	}
	return ""
}

func billHasClass(n *html.Node, class string) bool {
	return slices.Contains(strings.Fields(billAttribute(n, "class")), class)
}

func firstBillElement(root *html.Node, match func(*html.Node) bool) *html.Node {
	if root != nil {
		for n := range root.Descendants() {
			if n.Type == html.ElementNode && match(n) {
				return n
			}
		}
	}
	return nil
}

func billTableRows(table *html.Node) iter.Seq[*html.Node] {
	return func(yield func(*html.Node) bool) {
		for child := range table.ChildNodes() {
			switch child.DataAtom {
			case atom.Tr:
				if !yield(child) {
					return
				}
			case atom.Tbody, atom.Thead, atom.Tfoot:
				for row := range child.ChildNodes() {
					if row.DataAtom == atom.Tr && !yield(row) {
						return
					}
				}
			}
		}
	}
}

func billTopDivs(root *html.Node) []*html.Node {
	var divs []*html.Node
	var visit func(*html.Node)
	visit = func(parent *html.Node) {
		for n := range parent.ChildNodes() {
			if n.DataAtom == atom.Div {
				divs = append(divs, n)
			} else {
				visit(n)
			}
		}
	}
	visit(root)
	return divs
}

func billText(root *html.Node, skip func(*html.Node) bool) string {
	var out strings.Builder
	var visit func(*html.Node)
	visit = func(n *html.Node) {
		if n == nil || (skip != nil && skip(n)) {
			return
		}
		switch n.Type {
		case html.TextNode:
			out.WriteString(n.Data)
		case html.ElementNode:
			if n.DataAtom == atom.Script || n.DataAtom == atom.Style || n.DataAtom == atom.Template {
				return
			}
			out.WriteByte(' ')
			for child := range n.ChildNodes() {
				visit(child)
			}
			out.WriteByte(' ')
		}
	}
	visit(root)
	return strings.TrimSpace(billHTMLWhitespace.ReplaceAllString(out.String(), " "))
}

func billClassText(root *html.Node, class string) string {
	return billText(firstBillElement(root, func(n *html.Node) bool { return billHasClass(n, class) }), nil)
}

func billHistoryAnnotation(n *html.Node) bool {
	class := billAttribute(n, "class")
	return strings.Contains(class, "wth_payment") || strings.Contains(class, "wth_item_refunded")
}

func billStrikePrice(n *html.Node) bool {
	return n.DataAtom == atom.S || n.DataAtom == atom.Del
}

func billLicenseRemoveLink(n *html.Node) bool {
	return n.DataAtom == atom.A && billHasClass(n, "free_license_remove_link")
}
