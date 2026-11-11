package monitoring

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestMonitoringHTTPStateAndAllActions(t *testing.T) {
	s, _ := fixture(t)
	mux := http.NewServeMux()
	s.Register(mux)
	server := httptest.NewServer(mux)
	defer server.Close()
	for _, tc := range []struct{ action, state string }{{"track", "active"}, {"exclude", "excluded"}, {"include", "active"}, {"stop", "released"}} {
		res, e := http.Post(server.URL+"/api/v1/monitoring/"+tc.action, "application/json", strings.NewReader(`{"target_id":220}`))
		if e != nil {
			t.Fatal(e)
		}
		var body map[string]any
		e = json.NewDecoder(res.Body).Decode(&body)
		res.Body.Close()
		if e != nil || res.StatusCode != 200 || body["state"] != tc.state {
			t.Fatalf("%s response=%v status=%d err=%v", tc.action, body, res.StatusCode, e)
		}
		res, e = http.Get(server.URL + "/api/v1/monitoring/state?target_id=220")
		if e != nil {
			t.Fatal(e)
		}
		e = json.NewDecoder(res.Body).Decode(&body)
		res.Body.Close()
		if e != nil || body["state"] != tc.state {
			t.Fatalf("read=%v err=%v", body, e)
		}
	}
	res, e := http.Post(server.URL+"/api/v1/monitoring/track", "application/json", strings.NewReader(`{"target_type":"unsafe","target_id":220}`))
	if e != nil {
		t.Fatal(e)
	}
	res.Body.Close()
	if res.StatusCode != 422 {
		t.Fatalf("invalid type status=%d", res.StatusCode)
	}
}
