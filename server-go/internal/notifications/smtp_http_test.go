package notifications

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestSMTPHTTPEncryptsPasswordAndTestDoesNotWriteCandidates(t *testing.T) {
	var sent SMTPConfig
	s, db := deliveryFixture(t, func(_ context.Context, cfg SMTPConfig, _ string, _ string) (bool, string) {
		sent = cfg
		return true, ""
	})
	mux := http.NewServeMux()
	Register(mux, db, Options{Gate: s.gate, Delivery: s})
	request := func(method, path, body string) (int, map[string]any) {
		w := httptest.NewRecorder()
		r := httptest.NewRequest(method, path, strings.NewReader(body))
		mux.ServeHTTP(w, r)
		var v map[string]any
		if e := json.Unmarshal(w.Body.Bytes(), &v); e != nil {
			t.Fatal(w.Body.String())
		}
		return w.Code, v
	}
	code, v := request("PUT", "/api/v1/alerts/smtp", `{"host":"capture","port":465,"user":"sender@example.test","password":"fresh-test-password","toAddr":"capture@example.test","useSsl":true}`)
	if code != 200 || v["hasPassword"] != true || v["password"] == "fresh-test-password" {
		t.Fatalf("%d %v", code, v)
	}
	var stored string
	db.QueryRow(`SELECT value_json FROM app_settings WHERE key='smtp.password'`).Scan(&stored)
	if strings.Contains(stored, "fresh-test-password") || !strings.Contains(stored, "enc1:") {
		t.Fatal("password not encrypted")
	}
	code, _ = request("POST", "/api/v1/alerts/smtp/test", `{"host":"capture","port":465,"user":"sender@example.test","password":"","toAddr":"capture@example.test","useSsl":true}`)
	if code != 200 || sent.Password != "fresh-test-password" {
		t.Fatal(code)
	}
	code, _ = request("POST", "/api/v1/notifications/test", ``)
	if code != 200 {
		t.Fatal(code)
	}
	var count int
	db.QueryRow(`SELECT COUNT(*) FROM notification_candidates`).Scan(&count)
	if count != 1 {
		t.Fatal("test polluted business candidates")
	}
	code, _ = request("POST", "/api/v1/alerts/smtp/test", `{}`)
	if code != 400 {
		t.Fatal(code)
	}
}
