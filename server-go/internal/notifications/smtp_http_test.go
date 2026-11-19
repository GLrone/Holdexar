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

func TestTestMailSanitizesAuthAndConnectionErrors(t *testing.T) {
	problems := []string{
		"SMTPAuthenticationError: 535 Authentication failed for secret-user@example.test",
		"dial tcp 10.0.0.99:587: connect: connection refused",
	}
	for _, rawErr := range problems {
		s, db := deliveryFixture(t, func(_ context.Context, _ SMTPConfig, _ string, _ string) (bool, string) {
			return false, rawErr
		})
		mux := http.NewServeMux()
		Register(mux, db, Options{Gate: s.gate, Delivery: s})
		w := httptest.NewRecorder()
		r := httptest.NewRequest("POST", "/api/v1/alerts/smtp/test", strings.NewReader(`{"host":"10.0.0.99","port":587,"user":"sender@example.test","password":"top-secret-password","toAddr":"rcpt@example.test","useSsl":false}`))
		mux.ServeHTTP(w, r)
		var v map[string]any
		if e := json.Unmarshal(w.Body.Bytes(), &v); e != nil {
			t.Fatal(w.Body.String())
		}
		if w.Code != 400 || v["detail"] != "通知服务器连接失败，请检查地址与端口" {
			t.Fatalf("raw error leaked or wrong status: code=%d body=%v", w.Code, v)
		}
	}
	s, db := deliveryFixture(t, func(_ context.Context, _ SMTPConfig, _ string, _ string) (bool, string) {
		return false, "SMTPDeliveryUnknown: read tcp: i/o timeout"
	})
	mux := http.NewServeMux()
	Register(mux, db, Options{Gate: s.gate, Delivery: s})
	w := httptest.NewRecorder()
	r := httptest.NewRequest("POST", "/api/v1/alerts/smtp/test", strings.NewReader(`{"host":"10.0.0.99","port":587,"user":"sender@example.test","password":"top-secret-password","toAddr":"rcpt@example.test","useSsl":false}`))
	mux.ServeHTTP(w, r)
	var v map[string]any
	if e := json.Unmarshal(w.Body.Bytes(), &v); e != nil {
		t.Fatal(w.Body.String())
	}
	if w.Code != 400 || v["detail"] != "邮件发送结果无法确认，请先检查收件箱再决定是否重新测试" {
		t.Fatalf("unexpected delivery-unknown response: code=%d body=%v", w.Code, v)
	}
}
