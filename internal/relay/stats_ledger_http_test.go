// SPDX-License-Identifier: GPL-3.0-or-later
package relay

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

// The bucket is declared by the fixture BEFORE the request; status/error text
// alone cannot distinguish e.g. pre-Submit parse rejection from validation.
const ledgerValidationError = "raw transaction size must be 1..131072 bytes"
const ledgerConcurrencyError = "too many concurrent requests"

func ledgerErrorContract(response *httptest.ResponseRecorder, status int, want string) error {
	if response.Code != status || response.Header().Get("Content-Type") != "application/json" {
		return fmt.Errorf("unexpected response %d %q", response.Code, response.Body.String())
	}
	fields, err := soakStrictObject(response.Body.Bytes(), "error")
	if err != nil {
		return err
	}
	var message string
	if err := json.Unmarshal(fields["error"], &message); err != nil {
		return err
	}
	if message != want || want == "" {
		return fmt.Errorf("error message %q want %q", message, want)
	}
	return nil
}

func ledgerRecordError(t *testing.T, log *ledgerExpectation, response *httptest.ResponseRecorder, status, bucket int, want string) {
	t.Helper()
	if err := ledgerErrorContract(response, status, want); err != nil {
		t.Fatal(err)
	}
	if bucket >= 0 {
		log.counters[bucket]++
		if bucket != ledgerConcurrency {
			log.normalReturns++
		}
	}
}

func TestStatsLedgerExactErrorContracts(t *testing.T) {
	for _, tc := range []struct {
		status  int
		message string
	}{
		{400, ledgerValidationError}, {429, ledgerConcurrencyError},
	} {
		response := httptest.NewRecorder()
		response.Code = tc.status
		response.Header().Set("Content-Type", "application/json")
		body, err := json.Marshal(map[string]string{"error": tc.message})
		if err != nil {
			t.Fatal(err)
		}
		response.Body.Write(body)
		if err := ledgerErrorContract(response, tc.status, tc.message); err != nil {
			t.Fatal(err)
		}
		response.Body.Reset()
		response.Body.WriteString(`{"error":"wrong branch"}`)
		if ledgerErrorContract(response, tc.status, tc.message) == nil {
			t.Fatal("same status and error schema masked the wrong branch")
		}
	}
}

func TestStatsLedgerEarlyHTTPAndValidation(t *testing.T) {
	n := bareNode(t)
	h := n.Handler()
	before := ledgerHandlerSnapshot(t, h)
	log := ledgerExpectation{}
	for _, tc := range []struct{ body, reason string }{
		{`{`, "invalid JSON request"},
		{`{} {}`, "exactly one JSON object is required"},
		{`{"unexpected":1}`, "invalid JSON request"},
		{`{"raw_tx":"00"}`, "raw_tx must be 0x-prefixed hex"},
		{`{"raw_tx":"0xqq"}`, "invalid transaction hex"},
	} {
		ledgerRecordError(t, &log, statsRequest(h, "POST", "/v1/transactions", tc.body), 400, -1, tc.reason)
	}
	for _, tc := range []struct {
		method, host, origin, media string
		status                      int
		reason                      string
	}{
		{"GET", "localhost", "", "application/json", 405, "POST required"},
		{"POST", "evil.example", "", "application/json", 403, "invalid local Host header"},
		{"POST", "localhost", "https://example.com", "application/json", 403, "browser-origin requests are not allowed"},
		{"POST", "localhost", "", "text/plain", 415, "application/json required"},
	} {
		request := httptest.NewRequest(tc.method, "http://localhost/v1/transactions", strings.NewReader(`{"raw_tx":"0x"}`))
		request.Host = tc.host
		request.Header.Set("Content-Type", tc.media)
		request.Header.Set("Origin", tc.origin)
		response := httptest.NewRecorder()
		h.ServeHTTP(response, request)
		ledgerRecordError(t, &log, response, tc.status, -1, tc.reason)
	}
	// Valid JSON and hex, empty transaction bytes: reaches validation even
	// without peers. The earlier nine requests did not enter Submit.
	ledgerRecordError(t, &log, statsRequest(h, "POST", "/v1/transactions", `{"raw_tx":"0x"}`), 400, ledgerValidation, ledgerValidationError)
	if log != (ledgerExpectation{ledgerVector{ledgerValidation: 1}, 1, 0}) {
		t.Fatal("unexpected declared HTTP/Submit partition")
	}
	if err := ledgerCompare(before, ledgerHandlerSnapshot(t, h), log); err != nil {
		t.Fatal(err)
	}
}

func TestStatsLedgerIndependentHandlerSlots(t *testing.T) {
	n := bareNode(t)
	handlers := []http.Handler{n.Handler(), n.Handler()}
	before := ledgerHandlerSnapshot(t, handlers[0])
	entered := make(chan struct{}, 16)
	release := make(chan struct{})
	var unblock sync.Once
	defer unblock.Do(func() { close(release) })
	done := make(chan *httptest.ResponseRecorder, 16)
	for _, handler := range handlers {
		for i := 0; i < 8; i++ {
			go func() {
				request := httptest.NewRequest("POST", "http://localhost/v1/transactions", nil)
				request.Header.Set("Content-Type", "application/json")
				request.Body = &statsBlockedBody{entered: entered, release: release, reader: strings.NewReader(`{"raw_tx":"0x"}`)}
				response := httptest.NewRecorder()
				handler.ServeHTTP(response, request)
				done <- response
			}()
		}
	}
	// Entry into each body proves all 16 independently scoped slots acquired.
	// No Submit call exists yet; no concurrent sum assertion is needed here.
	for i := 0; i < 16; i++ {
		select {
		case <-entered:
		case <-time.After(3 * time.Second):
			t.Fatal("independent handler slots did not fill")
		}
	}
	log := ledgerExpectation{}
	for _, handler := range handlers {
		ledgerRecordError(t, &log, statsRequest(handler, "POST", "/v1/transactions", `{"raw_tx":"0x"}`), 429, ledgerConcurrency, ledgerConcurrencyError)
	}
	// A failed 429 response write still records the limiter's decision. This is
	// fixture-known execution, not a validated client response in uniqueCounts.
	writerRelease := make(chan struct{})
	close(writerRelease)
	failed := &ledgerGatedWriter{ResponseRecorder: httptest.NewRecorder(), entered: make(chan struct{}), release: writerRelease, fail: true}
	request := httptest.NewRequest("POST", "http://localhost/v1/transactions", strings.NewReader(`{"raw_tx":"0x"}`))
	request.Header.Set("Content-Type", "application/json")
	handlers[0].ServeHTTP(failed, request)
	if failed.Code != 429 || failed.writes != 1 {
		t.Fatal("failed writer did not reach the declared limiter response")
	}
	ledgerRecordError(t, &log, failed.ResponseRecorder, 429, ledgerConcurrency, ledgerConcurrencyError)
	unblock.Do(func() { close(release) })
	for i := 0; i < 16; i++ {
		select {
		case response := <-done:
			ledgerRecordError(t, &log, response, 400, ledgerValidation, ledgerValidationError)
		case <-time.After(3 * time.Second):
			t.Fatal("handler did not join")
		}
	}
	if log != (ledgerExpectation{ledgerVector{ledgerValidation: 16, ledgerConcurrency: 3}, 16, 0}) {
		t.Fatal("unexpected joined response ledger")
	}
	for _, handler := range handlers {
		if err := ledgerCompare(before, ledgerHandlerSnapshot(t, handler), log); err != nil {
			t.Fatal(err)
		}
	}
	// Recovered slots on both handlers and a fresh handler retain one Node epoch.
	for _, handler := range handlers {
		ledgerRecordError(t, &log, statsRequest(handler, "POST", "/v1/transactions", `{"raw_tx":"0x"}`), 400, ledgerValidation, ledgerValidationError)
	}
	if err := ledgerCompare(before, ledgerHandlerSnapshot(t, n.Handler()), log); err != nil {
		t.Fatal(err)
	}
}

type ledgerGatedWriter struct {
	*httptest.ResponseRecorder
	entered chan struct{}
	release <-chan struct{}
	fail    bool
	writes  int
}

func (w *ledgerGatedWriter) Write(payload []byte) (int, error) {
	w.writes++
	// Capture attempted bytes for fixture evidence, not client receipt.
	_, _ = w.ResponseRecorder.Write(payload)
	close(w.entered)
	<-w.release
	if w.fail {
		return 0, errors.New("fixture response write failure")
	}
	return len(payload), nil
}

func TestStatsLedgerResponseWriteBoundary(t *testing.T) {
	for _, fail := range []bool{false, true} {
		name := "normal_response"
		if fail {
			name = "failed_response_client_unknown"
		}
		t.Run(name, func(t *testing.T) {
			n := bareNode(t)
			addFakePeer(n, true, 1) // Admission only; no transport/remote claim.
			h := n.Handler()
			before := ledgerHandlerSnapshot(t, h)
			f := newUniqueGenerator(t).next(t, 0)
			release := make(chan struct{})
			var unblock sync.Once
			defer unblock.Do(func() { close(release) })
			writer := &ledgerGatedWriter{ResponseRecorder: httptest.NewRecorder(), entered: make(chan struct{}), release: release, fail: fail}
			request := httptest.NewRequest("POST", "http://localhost/v1/transactions", bytes.NewReader(f.body))
			request.Header.Set("Content-Type", "application/json")
			done := make(chan struct{})
			go func() { defer close(done); h.ServeHTTP(writer, request) }()
			select {
			case <-writer.entered:
			case <-time.After(3 * time.Second):
				t.Fatal("response writer barrier not reached")
			}
			// Write is reached only after Submit has normally returned. Do not
			// scrape-and-compare while the controlled Handler is still blocked.
			unblock.Do(func() { close(release) })
			select {
			case <-done:
			case <-time.After(3 * time.Second):
				t.Fatal("response writer did not join")
			}
			if writer.writes != 1 {
				t.Fatal("unexpected write count")
			}
			if _, _, err := uniqueValidateHTTP(writer.Code, writer.Header().Get("Content-Type"), writer.Body.Bytes(), f.soakRequest, uniqueAccept(false, 1, 1)); err != nil {
				t.Fatal(err)
			}
			after := ledgerHandlerSnapshot(t, h)
			fixtureKnown := ledgerExpectation{ledgerVector{ledgerNew: 1}, 1, 0}
			if err := ledgerCompare(before, after, fixtureKnown); err != nil {
				t.Fatal(err)
			}
			if fail {
				clientOnly := ledgerExpectation{unknown: 1}
				if ledgerCompare(before, after, clientOnly) == nil {
					t.Fatal("failed response invented a complete client ledger")
				}
				if ledgerCompare(before, after, ledgerExpectation{}) == nil {
					t.Fatal("failed response falsely proved no Submit execution")
				}
			}
		})
	}
}

func TestStatsLedgerSameClientErrorDifferentExecution(t *testing.T) {
	// The exact same transport error is compatible with zero OR one completed
	// Submit. Only fixture execution evidence disambiguates these two cases.
	for _, execute := range []bool{false, true} {
		name := "never_dispatched"
		if execute {
			name = "dispatched_response_lost"
		}
		t.Run(name, func(t *testing.T) {
			n := bareNode(t)
			addFakePeer(n, true, 1)
			h := n.Handler()
			before := ledgerHandlerSnapshot(t, h)
			f := newUniqueGenerator(t).next(t, 0)
			fixtureKnown := ledgerExpectation{}
			client := &http.Client{Timeout: time.Second, Transport: soakRoundTripFunc(func(request *http.Request) (*http.Response, error) {
				if execute {
					response := httptest.NewRecorder()
					h.ServeHTTP(response, request)
					if _, _, err := uniqueValidateHTTP(response.Code, response.Header().Get("Content-Type"), response.Body.Bytes(), f.soakRequest, uniqueAccept(false, 1, 1)); err != nil {
						return nil, err
					}
					fixtureKnown = ledgerExpectation{ledgerVector{ledgerNew: 1}, 1, 0}
				}
				_ = request.Body.Close()
				return nil, io.ErrUnexpectedEOF
			})}
			request, err := http.NewRequest("POST", "http://localhost/v1/transactions", bytes.NewReader(f.body))
			if err != nil {
				t.Fatal(err)
			}
			request.Header.Set("Content-Type", "application/json")
			if _, err := client.Do(request); !errors.Is(err, io.ErrUnexpectedEOF) {
				t.Fatalf("not the declared transport failure: %v", err)
			}
			after := ledgerHandlerSnapshot(t, h)
			if ledgerCompare(before, after, ledgerExpectation{unknown: 1}) == nil {
				t.Fatal("unknown client execution treated as a complete ledger")
			}
			if err := ledgerCompare(before, after, fixtureKnown); err != nil {
				t.Fatal(err)
			}
		})
	}
}
