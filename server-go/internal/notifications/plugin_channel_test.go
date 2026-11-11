package notifications

import (
	"archive/zip"
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"

	"holdexar/server-go/internal/plugins"
)

func notificationPlugin(t *testing.T, s *DeliveryService) *plugins.Manager {
	t.Helper()
	manifest := map[string]any{
		"id": "test.webhook", "name": "Test webhook", "version": "1.0.0", "author": "Test",
		"apiVersion": 1, "kind": "notification.webhook", "domains": []string{"hooks.example.com"},
		"endpoint": "https://hooks.example.com/messages", "body": map[string]any{"text": "{{subject}}\n{{body}}"},
		"configSchema": map[string]any{"type": "object", "properties": map[string]any{}},
	}
	var data bytes.Buffer
	w := zip.NewWriter(&data)
	f, e := w.Create("manifest.json")
	if e != nil {
		t.Fatal(e)
	}
	if e = json.NewEncoder(f).Encode(manifest); e != nil {
		t.Fatal(e)
	}
	if e = w.Close(); e != nil {
		t.Fatal(e)
	}
	m := plugins.New(s.db, s.gate, s.options.DataDir, plugins.Options{})
	if _, e = m.InstallLocal(context.Background(), data.Bytes()); e != nil {
		t.Fatal(e)
	}
	on := true
	if _, e = m.Update(context.Background(), "test.webhook", &on, map[string]any{}); e != nil {
		t.Fatal(e)
	}
	s.options.Plugins = m
	return m
}

func TestPluginDispatchUsesExistingCandidateAndKeepsMailTestSeparate(t *testing.T) {
	var messages, mails atomic.Int32
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		messages.Add(1)
		b, _ := io.ReadAll(r.Body)
		if r.URL.Path != "/messages" || !bytes.Contains(b, []byte("Portal 2")) {
			t.Errorf("bad delivery %s %s", r.URL, b)
		}
		w.WriteHeader(204)
	}))
	defer server.Close()
	s, db := deliveryFixture(t, func(context.Context, SMTPConfig, string, string) (bool, string) { mails.Add(1); return true, "" })
	notificationPlugin(t, s)
	transport := server.Client().Transport.(*http.Transport).Clone()
	transport.DialContext = func(ctx context.Context, _, _ string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, "tcp", server.Listener.Addr().String())
	}
	s.options.PluginHTTP = &http.Client{Transport: transport}
	result, e := s.Dispatch(context.Background(), nil)
	if e != nil || result.Sent != 1 || messages.Load() != 1 || mails.Load() != 0 {
		t.Fatalf("result=%+v err=%v messages=%d mails=%d", result, e, messages.Load(), mails.Load())
	}
	var recipient, status string
	if e = db.QueryRow(`SELECT recipient,status FROM notification_candidates`).Scan(&recipient, &status); e != nil {
		t.Fatal(e)
	}
	if !strings.HasPrefix(recipient, "plugin:test.webhook:") || status != "delivered" {
		t.Fatalf("%s %s", recipient, status)
	}
	if _, e = s.Dispatch(context.Background(), nil); e != nil || messages.Load() != 1 {
		t.Fatal("duplicate notification", e)
	}
	if e = s.TestMail(context.Background(), SMTPConfig{Host: "capture", User: "sender@example.test", Password: "test", ToAddr: "capture@example.test"}); e != nil || mails.Load() != 1 {
		t.Fatal("mail test was redirected", e)
	}
}

func TestPluginUnknownDeliveryDoesNotRetryOrFallBackToSMTP(t *testing.T) {
	var attempts, mails atomic.Int32
	s, db := deliveryFixture(t, func(context.Context, SMTPConfig, string, string) (bool, string) { mails.Add(1); return true, "" })
	notificationPlugin(t, s)
	s.options.PluginHTTP = &http.Client{Transport: notificationRoundTripper(func(r *http.Request) (*http.Response, error) {
		attempts.Add(1)
		return nil, io.ErrUnexpectedEOF
	})}
	result, e := s.Dispatch(context.Background(), nil)
	if e != nil || result.Unknown != 1 {
		t.Fatalf("%+v %v", result, e)
	}
	_, e = s.Dispatch(context.Background(), nil)
	var status string
	_ = db.QueryRow(`SELECT status FROM notification_candidates`).Scan(&status)
	if e != nil || attempts.Load() != 1 || mails.Load() != 0 || status != "unknown" {
		t.Fatalf("attempts=%d mails=%d status=%s err=%v", attempts.Load(), mails.Load(), status, e)
	}
}

type notificationRoundTripper func(*http.Request) (*http.Response, error)

func TestLastDeliveryBelongsToCurrentChannel(t *testing.T) {
	s, db := deliveryFixture(t, nil)
	if _, e := db.Exec(`UPDATE notification_candidates SET status='failed',last_error='SMTPAuthenticationError',updated_at='2026-11-06 12:00:00'`); e != nil {
		t.Fatal(e)
	}
	notificationPlugin(t, s)
	h := handler{db: db, options: Options{Delivery: s}}
	last, e := h.lastDelivery(httptest.NewRequest("GET", "/api/v1/notifications/prefs", nil))
	if e != nil || last != nil {
		t.Fatalf("old channel failure leaked into new channel: %+v %v", last, e)
	}
}

func TestPluginDisabledBeforeSendReleasesUnsentClaim(t *testing.T) {
	var sends atomic.Int32
	s, db := deliveryFixture(t, func(context.Context, SMTPConfig, string, string) (bool, string) { return false, "unexpected mail" })
	m := notificationPlugin(t, s)
	s.options.PluginHTTP = &http.Client{Transport: notificationRoundTripper(func(*http.Request) (*http.Response, error) {
		sends.Add(1)
		return &http.Response{StatusCode: 204, Body: io.NopCloser(strings.NewReader(""))}, nil
	})}
	original := s.options.Render
	s.options.Render = func(ctx context.Context, c []DeliveryCandidate, cycle *int64, p map[string]any) (string, string, error) {
		off := false
		if _, e := m.Update(ctx, "test.webhook", &off, nil); e != nil {
			return "", "", e
		}
		return original(ctx, c, cycle, p)
	}
	_, _ = s.Dispatch(context.Background(), nil)
	var status string
	var attempts int
	if e := db.QueryRow(`SELECT status,attempts FROM notification_candidates`).Scan(&status, &attempts); e != nil {
		t.Fatal(e)
	}
	if status != "pending" || attempts != 0 || sends.Load() != 0 {
		t.Fatalf("unsent notification was consumed: %s/%d sends=%d", status, attempts, sends.Load())
	}
	on := true
	if _, e := m.Update(context.Background(), "test.webhook", &on, nil); e != nil {
		t.Fatal(e)
	}
	s.options.Render = original
	result, e := s.Dispatch(context.Background(), nil)
	if e != nil || result.Sent != 1 || sends.Load() != 1 {
		t.Fatalf("did not recover %+v %v sends=%d", result, e, sends.Load())
	}
}

func (f notificationRoundTripper) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }
