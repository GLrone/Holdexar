package httpx

import (
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
)

func TestWriteJSONPreservesRawHTMLAndRejectsInvalidPayloadBeforeHeader(t *testing.T) {
	rec := httptest.NewRecorder()
	WriteJSON(rec, http.StatusCreated, map[string]string{"url": "https://a.example/x?b=1&c=<2>"})
	if rec.Code != http.StatusCreated {
		t.Fatalf("status = %d, want 201", rec.Code)
	}
	if got := rec.Header().Get("Content-Type"); got != "application/json" {
		t.Fatalf("Content-Type = %q", got)
	}
	if got := rec.Body.String(); got != `{"url":"https://a.example/x?b=1&c=<2>"}` {
		t.Fatalf("unexpected body: %s", got)
	}

	bad := httptest.NewRecorder()
	WriteJSON(bad, http.StatusOK, map[string]any{"fn": func() {}})
	if bad.Code != http.StatusInternalServerError {
		t.Fatalf("marshal error status = %d, want 500", bad.Code)
	}
}

func TestWriteValidationErrorAndQueryInt(t *testing.T) {
	ge, le := int64(1), int64(100)
	v, present, verr := QueryInt(url.Values{}, "limit", 20, &ge, &le)
	if v != 20 || present || verr != nil {
		t.Fatalf("default QueryInt = (%d, %v, %v)", v, present, verr)
	}
	if _, _, verr := QueryInt(url.Values{"limit": {"abc"}}, "limit", 20, &ge, &le); verr == nil || verr.Type != "int_parsing" {
		t.Fatalf("expected int_parsing, got %+v", verr)
	}
	if _, _, verr := QueryInt(url.Values{"limit": {"0"}}, "limit", 20, &ge, &le); verr == nil || verr.Type != "greater_than_equal" {
		t.Fatalf("expected greater_than_equal, got %+v", verr)
	}
	_, _, verr = QueryInt(url.Values{"limit": {"101"}}, "limit", 20, &ge, &le)
	if verr == nil || verr.Type != "less_than_equal" {
		t.Fatalf("expected less_than_equal, got %+v", verr)
	}
	rec := httptest.NewRecorder()
	WriteValidationError(rec, *verr)
	if rec.Code != http.StatusUnprocessableEntity || !strings.Contains(rec.Body.String(), `"less_than_equal"`) {
		t.Fatalf("422 body = %d %s", rec.Code, rec.Body.String())
	}
	nullBody := httptest.NewRecorder()
	WriteJSON(nullBody, http.StatusInternalServerError, DetailNull())
	if nullBody.Body.String() != `{"detail":null}` {
		t.Fatalf("detail null = %s", nullBody.Body.String())
	}
}
