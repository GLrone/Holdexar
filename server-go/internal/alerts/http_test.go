package alerts

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestHTTPRuleWriteAndVisibleHistory(t *testing.T) {
	s, db := fixture(t)
	observed(t, db)
	mux := http.NewServeMux()
	s.Register(mux)
	request := func(method, path, body string) (int, any) {
		t.Helper()
		w := httptest.NewRecorder()
		r := httptest.NewRequest(method, path, strings.NewReader(body))
		mux.ServeHTTP(w, r)
		var v any
		if e := json.Unmarshal(w.Body.Bytes(), &v); e != nil {
			t.Fatal(w.Body.String())
		}
		return w.Code, v
	}
	code, _ := request("POST", "/api/v1/alerts", `{"appid":620,"region":"US","targetType":"price","targetValue":999,"config":{"currency":"USD"}}`)
	if code != 200 {
		t.Fatal(code)
	}
	code, value := request("GET", "/api/v1/alerts/events", "")
	if code != 200 || len(value.([]any)) != 1 {
		t.Fatalf("%d %#v", code, value)
	}
	event := value.([]any)[0].(map[string]any)
	if event["price"] != float64(999) || event["priceText"] != "$9.99" || event["notified"] != false {
		t.Fatal(event)
	}
	if code, _ = request("DELETE", "/api/v1/alerts/events/1", ""); code != 409 {
		t.Fatal(code)
	}
	if code, _ = request("POST", "/api/v1/alerts", `{"appid":620,"region":"US","targetType":"price","targetValue":1.5}`); code != 400 {
		t.Fatal(code)
	}
	if code, _ = request("POST", "/api/v1/alerts/preview", `{"appid":620,"region":"US","targetType":"pct","targetValue":50}`); code != 200 {
		t.Fatal(code)
	}
	if code, _ = request("PUT", "/api/v1/alerts/999", `{"active":false}`); code != 404 {
		t.Fatal(code)
	}
}
