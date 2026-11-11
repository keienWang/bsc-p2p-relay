// SPDX-License-Identifier: GPL-3.0-or-later
package relay

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// Exercise the real HTTP/1 response writer, not ResponseRecorder: exceeding
// MaxBytesReader's limit must reject before Submit and close that connection.
// This is a relay contract test, not a reproducer of the upstream concurrent
// body-read/header-write race. Node.Handler currently reads and writes in order.
func TestHTTPBodyLimitClosesConnection(t *testing.T) {
	n := bareNode(t)
	server := httptest.NewServer(n.Handler())
	defer server.Close()
	client := server.Client()
	client.Timeout = 5 * time.Second
	defer client.CloseIdleConnections()

	// A valid JSON prefix forces the decoder to consume beyond the body limit.
	body := `{"raw_tx":"0x` + strings.Repeat("0", 2*MaxRawTransaction+1024) + `"}`
	for attempt := 0; attempt < 9; attempt++ {
		response, err := client.Post(server.URL+"/v1/transactions", "application/json", strings.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		payload, readErr := io.ReadAll(io.LimitReader(response.Body, 4096))
		closeErr := response.Body.Close()
		if readErr != nil || closeErr != nil {
			t.Fatalf("read/close response: %v / %v", readErr, closeErr)
		}
		if response.ProtoMajor != 1 || response.StatusCode != http.StatusBadRequest || !response.Close {
			t.Fatalf("want HTTP/1 400 with connection closed, got %s %s close=%t", response.Proto, response.Status, response.Close)
		}
		if string(payload) != "{\"error\":\"invalid JSON request\"}\n" || response.Header.Get("Content-Type") != "application/json" {
			t.Fatalf("unexpected error response: %s %q", response.Header.Get("Content-Type"), payload)
		}
	}
	// More requests than the eight admission slots prove rejected bodies release
	// their slot. No transaction decoding, queue admission or peer is involved.
	if got := n.Stats(); got.Submit != (SubmitStats{}) || got.HTTP.ConcurrencyRejectedTotal != 0 {
		t.Fatalf("oversized bodies entered Submit or leaked an HTTP slot: %+v", got)
	}
	response, err := client.Get(server.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("server unhealthy after body rejection: %s", response.Status)
	}
}
