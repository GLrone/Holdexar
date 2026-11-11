package bills

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"reflect"
	"testing"

	"holdexar/server-go/internal/store"
)

func TestBillsHTTPViews(t *testing.T) {
	path := filepath.Join(t.TempDir(), "holdexar.db")
	db, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := store.InitializeFreshDatabase(context.Background(), db); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`
INSERT INTO bill_imports VALUES
 (1,NULL,'Old','b64','old.json','2026-10-01 10:00:00.000000','["warning"]'),
 (2,'765','Owned','','',NULL,'[]'),
 (3,'766','Second','data:image/png;base64,saved','',NULL,'');
INSERT INTO bill_game_txs VALUES
 (1,1,'2025-12-30','buy','["Game A"]','CNY',30,1,3000,1,'','Wallet','',NULL,'','',NULL,NULL,0,0,0,1),
 (2,1,'2026-01-03','gift','["Gift Game"]','CNY',50,1,5000,1,'','Mixed','-50%',100,'¥50','¥100','[{"pm":"Wallet","amount":"¥10"}]','["friend"]',1,0,0,2),
 (3,1,'2026-01-03','refund','["Gift Game"]','CNY',50,-1,-5000,1,'','','',NULL,'','','{broken','{"not":"a list"}',0,1,1,3),
 (4,1,'2026-02-04','buy','["Unknown"]','USD',10,1,NULL,NULL,'missing','','',NULL,'','',NULL,NULL,0,0,0,4),
 (5,2,'2026-02-04','buy','[]','CNY',1,1,100,1,'','','',NULL,'','',NULL,NULL,0,0,0,5),
 (6,2,'2026-02-05','refund','[]','CNY',0.5,-1,50,1,'','','',NULL,'','',NULL,NULL,0,1,0,6);
INSERT INTO bill_topup_txs VALUES
 (1,1,'2026-01-01','topup','CNY',60,1,6000,1,'','Wallet','topup',0),
 (2,1,'2026-01-02','refund','CNY',10,-1,-1000,1,'','','refund',1),
 (3,1,'2026-01-03','unknown','USD',5,1,NULL,NULL,'missing','','topup',0);
INSERT INTO bill_cdk_games VALUES
 (1,1,'CDK Game',1234,'2026-01-04','cdk','Retail',NULL),
 (2,1,'Free Game',NULL,'2026-01-05','free','Free',9999),
 (3,1,'Gift Game',NULL,'2026-01-05','gift','Gift',0);
`); err != nil {
		t.Fatal(err)
	}
	mux := http.NewServeMux()
	RegisterViews(mux, db, store.NewWriteGate(db, path))
	server := httptest.NewServer(mux)
	defer server.Close()
	request := func(method, path, body string, status int) any {
		t.Helper()
		req, err := http.NewRequest(method, server.URL+"/api/v1/bills"+path, bytes.NewBufferString(body))
		if err != nil {
			t.Fatal(err)
		}
		req.Header.Set("Content-Type", "application/json")
		res, err := server.Client().Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer res.Body.Close()
		data, err := io.ReadAll(res.Body)
		if err != nil {
			t.Fatal(err)
		}
		if res.StatusCode != status {
			t.Fatalf("%s %s: got %d, want %d: %s", method, path, res.StatusCode, status, data)
		}
		var result any
		if err := json.Unmarshal(data, &result); err != nil {
			t.Fatalf("%s %s: invalid JSON: %s", method, path, data)
		}
		return result
	}
	assert := func(got any, want string) {
		t.Helper()
		var expected any
		if err := json.Unmarshal([]byte(want), &expected); err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(got, expected) {
			b, _ := json.Marshal(got)
			t.Fatalf("got %s\nwant %s", b, want)
		}
	}
	object := func(v any) map[string]any {
		t.Helper()
		obj, ok := v.(map[string]any)
		if !ok {
			t.Fatalf("expected object: %#v", v)
		}
		return obj
	}

	imports := object(request("GET", "/imports", "", 200))["imports"].([]any)
	assert(imports[0], `{"id":3,"steamId":"766","nickname":"Second","sourceFile":"","importedAt":null,"gameNetFen":0,"gameSpendFen":0,"gameRefundFen":0,"orders":0,"fxMissing":0}`)
	assert(imports[2], `{"id":1,"steamId":null,"nickname":"Old","sourceFile":"old.json","importedAt":"2026-10-01T10:00:00","gameNetFen":3000,"gameSpendFen":8000,"gameRefundFen":5000,"orders":3,"fxMissing":1}`)
	overview := object(request("GET", "/imports/1/overview", "", 200))
	assert(overview, `{"id":1,"steamId":null,"nickname":"Old","sourceFile":"old.json","importedAt":"2026-10-01T10:00:00","avatar":"data:image/png;base64,b64","warnings":["warning"],
 "summary":{"netFen":3000,"spendFen":8000,"refundFen":5000,"orders":3,"txCount":4,"fxMissing":1,
 "buyTotalFen":3000,"buyRefundFen":0,"giftTotalFen":5000,"giftRefundFen":5000,"selfNetFen":3000,"giftNetFen":0,"quotaFen":3000,
 "accountValueFen":3000,"cdkPriced":1,"cdkTotalFen":0,"topupNetFen":5000,"topupSpendFen":6000,"topupRefundFen":1000},
 "years":[{"year":"2026","netFen":0,"spendFen":5000,"refundFen":5000,"count":3,"months":{"01":{"netFen":0,"count":2},"02":{"netFen":0,"count":1}}},
 {"year":"2025","netFen":3000,"spendFen":3000,"refundFen":0,"count":1,"months":{"12":{"netFen":3000,"count":1}}}],
 "monthSeries":[{"month":"2025-12","netFen":3000,"spendFen":3000,"refundFen":0,"count":1},
 {"month":"2026-01","netFen":0,"spendFen":5000,"refundFen":5000,"count":2},{"month":"2026-02","netFen":0,"spendFen":0,"refundFen":0,"count":1}],
 "topupByCurrency":[{"currency":"CNY","total":50,"cnyFen":5000,"count":2},{"currency":"USD","total":5,"cnyFen":0,"count":1}],
 "counts":{"gameTxs":4,"topupTxs":3,"cdkGames":3,"cdk":1,"gift":1,"free":1}}`)
	empty := object(request("GET", "/imports/3/overview", "", 200))
	assert(empty["avatar"], `"data:image/png;base64,saved"`)
	for _, key := range []string{"warnings", "years", "monthSeries", "topupByCurrency"} {
		assert(empty[key], `[]`)
	}
	assert(request("GET", "/imports/1/game-txs?year=2026&month=2026-01&search=gift%20game&limit=1&offset=1", "", 200),
		`{"total":2,"limit":1,"offset":1,"rows":[{"id":2,"date":"2026-01-03","txType":"gift","items":["Gift Game"],"currency":"CNY","amount":50,"sign":1,"cnyFen":5000,"fxRate":1,"fxNote":"","payment":"Mixed","discountPct":"-50%","origPrice":100,"walletBalance":"¥50","basePrice":"¥100","paymentParts":[{"pm":"Wallet","amount":"¥10"}],"giftTo":["friend"],"isGift":true,"isRefund":false,"origIsGift":false}]}`)
	refund := object(request("GET", "/imports/1/game-txs?txType=refund", "", 200))
	assert(refund, `{"total":1,"limit":200,"offset":0,"rows":[{"id":3,"date":"2026-01-03","txType":"refund","items":["Gift Game"],"currency":"CNY","amount":50,"sign":-1,"cnyFen":-5000,"fxRate":1,"fxNote":"","payment":"","discountPct":"","origPrice":null,"walletBalance":null,"basePrice":null,"paymentParts":null,"giftTo":null,"isGift":false,"isRefund":true,"origIsGift":true}]}`)
	assert(request("GET", "/imports/1/game-txs?limit=0", "", 200), `{"total":4,"limit":0,"offset":0,"rows":[]}`)
	assert(request("GET", "/imports/1/game-txs?year=2025&month=2026-01", "", 200), `{"total":0,"limit":200,"offset":0,"rows":[]}`)
	assert(request("GET", "/imports/1/game-txs?year=2025&year=2026&month=2026-02&limit=1&limit=0&offset=-2", "", 200), `{"total":1,"limit":0,"offset":-2,"rows":[]}`)
	assert(request("GET", "/imports/1/topup-txs?limit=1", "", 200), `{"total":3,"limit":1,"offset":0,"rows":[{"id":3,"date":"2026-01-03","desc":"unknown","currency":"USD","amount":5,"sign":1,"cnyFen":null,"fxRate":null,"payment":"","txType":"topup","isRefund":false}]}`)
	assert(request("GET", "/imports/1/cdk?acq=gift&search=GAME", "", 200), `{"total":1,"limit":500,"offset":0,"rows":[{"id":3,"name":"Gift Game","appid":null,"date":"2026-01-05","acq":"gift","acqLabel":"Gift","manualFen":0}]}`)
	assert(request("GET", "/imports/1/cdk?acq=cdk", "", 200), `{"total":1,"limit":500,"offset":0,"rows":[{"id":1,"name":"CDK Game","appid":1234,"date":"2026-01-04","acq":"cdk","acqLabel":"Retail","manualFen":null}]}`)
	assert(request("GET", "/imports/1/game-txs?limit=bad", "", 422), `{"detail":[{"type":"int_parsing","loc":["query","limit"],"msg":"Input should be a valid integer, unable to parse string as an integer","input":"bad"}]}`)
	request("GET", "/imports/not-an-id/overview", "", 422)
	request("PUT", "/cdk/1/price", "", 422)
	assert(request("PUT", "/cdk/1/price", `{"manualFen":2.5}`, 422), `{"detail":[{"type":"int_from_float","loc":["body","manualFen"],"msg":"Input should be a valid integer, got a number with a fractional part","input":2.5}]}`)
	assert(request("PUT", "/cdk/1/price", `{"manualFen":1500}`, 200), `{"id":1,"name":"CDK Game","manualFen":1500}`)
	priced := object(object(request("GET", "/imports/1/overview", "", 200))["summary"])
	assert(priced["accountValueFen"], "4500")
	assert(priced["cdkPriced"], "2")
	assert(request("PUT", "/cdk/1/price", `{"manual_fen":null}`, 200), `{"id":1,"name":"CDK Game","manualFen":null}`)
	assert(request("PUT", "/cdk/1/price", `{"manualFen":0,"manual_fen":13}`, 200), `{"id":1,"name":"CDK Game","manualFen":0}`)
	assert(request("PUT", "/cdk/1/price", `{}`, 200), `{"id":1,"name":"CDK Game","manualFen":null}`)
	assert(request("PUT", "/cdk/999/price", `{}`, 404), `{"detail":"'CDK 条目 999 不存在'"}`)

	if _, err := db.Exec(`CREATE TRIGGER hold_import BEFORE DELETE ON bill_imports WHEN old.id=1 BEGIN SELECT RAISE(ABORT,'hold'); END`); err != nil {
		t.Fatal(err)
	}
	request("DELETE", "/imports/1", "", 500)
	assert(request("GET", "/imports/1/overview", "", 200), string(mustMarshal(t, overview)))
	if _, err := db.Exec(`DROP TRIGGER hold_import`); err != nil {
		t.Fatal(err)
	}
	assert(request("DELETE", "/imports/1", "", 200), `{"deleted":true,"importId":1}`)
	assert(request("DELETE", "/imports/1", "", 200), `{"deleted":false,"importId":1}`)
	assert(request("GET", "/imports/1/overview", "", 404), `{"detail":"'账单 1 不存在'"}`)
	assert(request("GET", "/imports/1/game-txs", "", 200), `{"total":0,"limit":200,"offset":0,"rows":[]}`)
	assert(request("GET", "/imports/1/topup-txs", "", 200), `{"total":0,"limit":200,"offset":0,"rows":[]}`)
	assert(request("GET", "/imports/1/cdk", "", 200), `{"total":0,"limit":500,"offset":0,"rows":[]}`)
	other := object(request("GET", "/imports/2/overview", "", 200))
	assert(object(other["summary"])["netFen"], "50")
	assert(other["monthSeries"], `[{"month":"2026-02","netFen":150,"spendFen":100,"refundFen":50,"count":2}]`)
}

func mustMarshal(t *testing.T, value any) []byte {
	t.Helper()
	data, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return data
}
