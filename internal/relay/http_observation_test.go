// SPDX-License-Identifier: GPL-3.0-or-later
package relay

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

type observationUnreadBody struct {
	testing *testing.T
}

func (b observationUnreadBody) Read([]byte) (int, error) {
	b.testing.Fatal("observation handler attempted to read the submission body")
	return 0, nil
}
func (observationUnreadBody) Close() error { return nil }

func TestObservationHandler(t *testing.T) {
	node, err := New(DefaultConfig())
	if err != nil {
		t.Fatal(err)
	}
	defer node.Close()
	handler := node.ObservationHandler()
	for _, tc := range []struct {
		path string
		want int
	}{
		{"/healthz", http.StatusOK},
		{"/readyz", http.StatusServiceUnavailable},
		{"/v1/stats", http.StatusOK},
	} {
		t.Run(tc.path, func(t *testing.T) {
			w := httptest.NewRecorder()
			handler.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "http://127.0.0.1"+tc.path, nil))
			if w.Code != tc.want || w.Header().Get("Cache-Control") != "no-store" {
				t.Fatalf("%s: status %d, headers %v", tc.path, w.Code, w.Header())
			}
		})
	}

	before := node.Stats()
	w := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "http://127.0.0.1/v1/transactions", observationUnreadBody{t})
	req.Header.Set("Content-Type", "application/json")
	handler.ServeHTTP(w, req)
	if w.Code != http.StatusForbidden || !strings.Contains(w.Body.String(), "submission is disabled") {
		t.Fatalf("observation submission: status %d, body %s", w.Code, w.Body)
	}
	after := node.Stats()
	if before.Submit != after.Submit || before.HTTP != after.HTTP {
		t.Fatal("rejected request passed through to submission or concurrency admission")
	}

	w = httptest.NewRecorder()
	req = httptest.NewRequest(http.MethodPost, "http://127.0.0.1/v1/transactions", strings.NewReader("{}"))
	req.Header.Set("Content-Type", "application/json")
	node.Handler().ServeHTTP(w, req)
	if w.Code != http.StatusBadRequest || !strings.Contains(w.Body.String(), "0x-prefixed") {
		t.Fatalf("standard submission path changed: status %d, body %s", w.Code, w.Body)
	}
}
