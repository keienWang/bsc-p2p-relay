// SPDX-License-Identifier: GPL-3.0-or-later
package relay

import (
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

type disconnectObservedBody struct {
	io.ReadCloser
	once    sync.Once
	entered chan<- struct{}
}

func (b *disconnectObservedBody) Read(p []byte) (int, error) {
	b.once.Do(func() { b.entered <- struct{}{} })
	return b.ReadCloser.Read(p)
}

// Real HTTP/1 connections exercise net/http's interrupted body reads. Recorder
// tests with artificial readers cannot establish that a disconnected client
// releases the admission slot, or that observation remains available under load.
func TestHTTPDisconnectedBodiesReleaseAdmission(t *testing.T) {
	n := bareNode(t)
	handler := n.Handler()
	entered := make(chan struct{}, 8)
	completed := make(chan struct{}, 32)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/v1/transactions" {
			r.Body = &disconnectObservedBody{ReadCloser: r.Body, entered: entered}
			defer func() { completed <- struct{}{} }()
		}
		handler.ServeHTTP(w, r)
	}))
	t.Cleanup(server.Close)
	client := server.Client()
	client.Timeout = 5 * time.Second
	defer client.CloseIdleConnections()
	wait := func(ch <-chan struct{}, label string) {
		t.Helper()
		select {
		case <-ch:
		case <-time.After(5 * time.Second):
			t.Fatal(label)
		}
	}
	for cycle := 0; cycle < 2; cycle++ {
		connections := make([]net.Conn, 0, 8)
		for slot := 0; slot < 8; slot++ {
			conn, err := net.DialTimeout("tcp", server.Listener.Addr().String(), 5*time.Second)
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { conn.Close() })
			if err := conn.SetDeadline(time.Now().Add(10 * time.Second)); err != nil {
				t.Fatal(err)
			}
			connections = append(connections, conn)
			// Deliberately incomplete JSON and body: the decoder must await more data.
			if _, err := fmt.Fprintf(conn, "POST /v1/transactions HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{\"raw_tx\":"); err != nil {
				t.Fatal(err)
			}
			wait(entered, "body did not enter admission slot")
		}
		response, err := client.Post(server.URL+"/v1/transactions", "application/json", strings.NewReader(`{}`))
		if err != nil {
			t.Fatal(err)
		}
		payload, err := io.ReadAll(response.Body)
		response.Body.Close()
		if err != nil || response.StatusCode != 429 || !strings.Contains(string(payload), "too many concurrent requests") {
			t.Fatalf("saturation response: %s %q %v", response.Status, payload, err)
		}
		wait(completed, "saturated request did not complete")
		for _, path := range []string{"/healthz", "/readyz", "/v1/stats"} {
			response, err = client.Get(server.URL + path)
			if err != nil {
				t.Fatal(err)
			}
			_, err = io.Copy(io.Discard, response.Body)
			response.Body.Close()
			wantStatus := http.StatusOK
			if path == "/readyz" {
				wantStatus = http.StatusServiceUnavailable
			}
			if err != nil || response.StatusCode != wantStatus {
				t.Fatalf("observation while saturated: %s %v", response.Status, err)
			}
		}
		for _, conn := range connections {
			if err := conn.Close(); err != nil {
				t.Fatal(err)
			}
		}
		for range connections {
			wait(completed, "disconnected request retained its admission slot")
		}
	}
	// Both cycles acquire all eight slots. No partial JSON reached Submit.
	if got := n.Stats(); got.Submit != (SubmitStats{}) || got.HTTP.ConcurrencyRejectedTotal != 2 {
		t.Fatalf("unexpected ledger after disconnect recovery: %+v", got)
	}
	response, err := client.Post(server.URL+"/v1/transactions", "application/json", strings.NewReader(`{}`))
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != 400 {
		t.Fatalf("admission did not recover after final cycle: %s", response.Status)
	}
	wait(entered, "final request did not acquire a slot")
	wait(completed, "final request did not complete")
}
