package access

import (
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

type lanAccessContract struct {
	Cases []lanAccessCase `json:"cases"`
}

type lanAccessCase struct {
	ID         string            `json:"id"`
	LANEnabled bool              `json:"lan_enabled"`
	PeerIP     string            `json:"peer_ip"`
	Host       string            `json:"host"`
	Method     string            `json:"method"`
	Path       string            `json:"path"`
	Headers    map[string]string `json:"headers"`
	WantStatus int               `json:"want_status"`
}

func TestLANAccessContract(t *testing.T) {
	contract := readLANAccessContract(t)
	if len(contract.Cases) == 0 {
		t.Fatal("LAN access contract has no cases")
	}

	for _, tc := range contract.Cases {
		t.Run(tc.ID, func(t *testing.T) {
			resetLAN(t)
			t.Setenv(EnvAccess, "off")
			SetExtraHosts(nil)
			SetLANEnabled(tc.LANEnabled)
			if tc.LANEnabled {
				SetExtraHosts([]string{"192.168.1.5"})
			}

			gate := New()
			handler := gate.Middleware(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				w.WriteHeader(http.StatusOK)
			}))
			req := httptest.NewRequest(tc.Method, "http://contract.test"+tc.Path, nil)
			req.Host = tc.Host
			req.RemoteAddr = net.JoinHostPort(tc.PeerIP, "51234")
			for name, value := range tc.Headers {
				req.Header.Set(name, value)
			}

			rec := httptest.NewRecorder()
			handler.ServeHTTP(rec, req)
			if rec.Code != tc.WantStatus {
				t.Fatalf("%s %s peer=%s host=%s: status=%d, want %d; body=%s", tc.Method, tc.Path, tc.PeerIP, tc.Host, rec.Code, tc.WantStatus, rec.Body.String())
			}
		})
	}
}

func readLANAccessContract(t *testing.T) lanAccessContract {
	t.Helper()
	_, source, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("cannot locate LAN contract test source")
	}
	path := filepath.Join(filepath.Dir(source), "..", "..", "..", "tests", "contracts", "lan_access.json")
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read LAN access contract: %v", err)
	}
	var contract lanAccessContract
	if err := json.Unmarshal(data, &contract); err != nil {
		t.Fatalf("decode LAN access contract: %v", err)
	}
	return contract
}
