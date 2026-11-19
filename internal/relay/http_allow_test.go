// SPDX-License-Identifier: GPL-3.0-or-later
package relay

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// RFC 9110 section 15.5.6 requires supported methods on a 405 response.
// Advertising a method must not enable OPTIONS/CORS or override observation mode.
func TestHTTPMethodAllowHeaders(t *testing.T) {
	for _, observe := range []bool{false, true} {
		n := bareNode(t)
		handler := n.Handler()
		if observe {
			handler = n.ObservationHandler()
		}
		for _, path := range []string{"/healthz", "/readyz", "/v1/stats", "/v1/transactions"} {
			for _, method := range []string{http.MethodOptions, http.MethodPut, http.MethodDelete} {
				t.Run(path+"/"+method, func(t *testing.T) {
					request := httptest.NewRequest(method, "http://localhost"+path, strings.NewReader("ignored body"))
					response := httptest.NewRecorder()
					handler.ServeHTTP(response, request)
					wantStatus, wantAllow, wantError := http.StatusMethodNotAllowed, http.MethodGet, "GET required"
					if path == "/v1/transactions" {
						wantAllow, wantError = http.MethodPost, "POST required"
						if observe {
							wantStatus, wantAllow, wantError = http.StatusForbidden, "", "transaction submission is disabled in observation mode"
						}
					}
					if response.Code != wantStatus || response.Header().Get("Allow") != wantAllow {
						t.Fatalf("status/Allow = %d/%q; want %d/%q", response.Code, response.Header().Get("Allow"), wantStatus, wantAllow)
					}
					var body map[string]string
					if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil || len(body) != 1 || body["error"] != wantError {
						t.Fatalf("changed error body %q: %v", response.Body.String(), err)
					}
					if response.Header().Get("Access-Control-Allow-Methods") != "" || response.Header().Get("Cache-Control") != "no-store" {
						t.Fatalf("unexpected CORS/cache headers: %v", response.Header())
					}
				})
			}
		}
		if got := n.Stats(); got.Submit != (SubmitStats{}) || got.HTTP != (HTTPStats{}) {
			t.Fatalf("method rejection entered submission: %+v", got)
		}
	}
}

func TestHTTPMethodAllowOnlyOnMethodRejection(t *testing.T) {
	n := bareNode(t)
	for _, tc := range []struct {
		method, path, host, origin string
		status                     int
	}{
		{"GET", "/healthz", "localhost", "", 200},
		{"GET", "/readyz", "localhost", "", 503},
		{"GET", "/v1/stats", "localhost", "", 200},
		{"GET", "/unknown", "localhost", "", 404},
		{"POST", "/v1/transactions", "localhost", "", 415},
		{"PUT", "/v1/stats", "example.invalid", "", 403},
		{"PUT", "/v1/stats", "localhost", "https://example.invalid", 403},
	} {
		t.Run(tc.method+tc.path+tc.host+tc.origin, func(t *testing.T) {
			request := httptest.NewRequest(tc.method, "http://localhost"+tc.path, nil)
			request.Host = tc.host
			request.Header.Set("Origin", tc.origin)
			response := httptest.NewRecorder()
			n.Handler().ServeHTTP(response, request)
			if response.Code != tc.status || response.Header().Get("Allow") != "" {
				t.Fatalf("status/Allow = %d/%q; want %d/empty", response.Code, response.Header().Get("Allow"), tc.status)
			}
		})
	}
}
