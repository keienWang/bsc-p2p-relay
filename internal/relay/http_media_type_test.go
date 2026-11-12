// SPDX-License-Identifier: GPL-3.0-or-later
package relay

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestTransactionContentType(t *testing.T) {
	tests := []struct {
		name  string
		media string
		valid bool
	}{
		{"plain", "application/json", true},
		{"mixed case", "Application/JSON", true},
		{"parameter whitespace", "application/json ; charset=utf-8", true},
		{"surrounding whitespace", " application/json ", true},
		{"quoted parameter", "application/json; charset=\"utf-8\"", true},
		{"quoted semicolon", "application/json; profile=\"one;two\"", true},
		{"mixed case parameter", "APPLICATION/JSON; CHARSET=UTF-8", true},
		{"missing", "", false},
		{"other type", "text/plain", false},
		{"json suffix", "application/problem+json", false},
		{"multiple types", "application/json, text/plain", false},
		{"missing parameter value", "application/json; charset=", false},
		{"missing parameter equals", "application/json; charset", false},
		{"unterminated quoted parameter", "application/json; charset=\"utf-8", false},
		{"conflicting parameters", "application/json; charset=utf-8; charset=ascii", false},
		{"conflicting case parameters", "application/json; charset=utf-8; CHARSET=ascii", false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			// Invalid hex stops before Submit: no node setup, signing, or P2P.
			n := &Node{}
			req := httptest.NewRequest(http.MethodPost, "http://127.0.0.1/v1/transactions", strings.NewReader(`{"raw_tx":"0xzz"}`))
			req.Header.Set("Content-Type", tt.media)
			response := httptest.NewRecorder()
			n.Handler().ServeHTTP(response, req)

			wantStatus, wantError := http.StatusUnsupportedMediaType, "application/json required"
			if tt.valid {
				wantStatus, wantError = http.StatusBadRequest, "invalid transaction hex"
			}
			if response.Code != wantStatus {
				t.Fatalf("status = %d, want %d; body = %s", response.Code, wantStatus, response.Body.String())
			}
			var body map[string]string
			if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil {
				t.Fatal(err)
			}
			if len(body) != 1 || body["error"] != wantError {
				t.Fatalf("body = %v, want only error %q", body, wantError)
			}
			stats := n.Stats()
			if stats.Submit != (SubmitStats{}) || stats.HTTP != (HTTPStats{}) {
				t.Fatalf("pre-Submit rejection changed counters: %+v", stats)
			}
		})
	}
}
