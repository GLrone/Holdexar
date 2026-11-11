package bills

import "encoding/json"

type historyRow struct {
	Date, Item, Type, Payment, Total, WalletChange, WalletBalance, BasePrice, OriginalPrice, Discount string
	TypeCount                                                                                         int
	PaymentParts                                                                                      json.RawMessage
	GiftRecipients                                                                                    []string
}

type licenseRow struct {
	Date, Item, Method string
	AppID              *int64
}

type steamReport struct {
	Nickname, Avatar string
	History          []historyRow
	Licenses         []licenseRow
}

type parsedReport struct {
	Nickname, Avatar string
	Game             []gameTx
	Topup            []topupTx
	Licenses         []licenseTx
	Warnings         []string
}

type gameTx struct {
	Date, Type, Currency, Payment, Discount, WalletBalance, BasePrice string
	Items, GiftTo                                                     []string
	PaymentParts                                                      json.RawMessage
	Amount                                                            float64
	OrigPrice                                                         *float64
	Sign, RawIdx                                                      int
	Gift, Refund, OrigGift                                            bool
}

type topupTx struct {
	Date, Desc, Currency, Payment, Type string
	Amount                              float64
	Sign                                int
	Refund                              bool
}

type licenseTx struct {
	Name, Date, Acq, AcqLabel string
	AppID, ManualFen          *int64
}
