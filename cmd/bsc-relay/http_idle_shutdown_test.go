// SPDX-License-Identifier: GPL-3.0-or-later
package main

import (
	"bufio"
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

// An idle keep-alive client must see EOF rather than survive graceful shutdown.
// Use a raw loopback socket so client pooling/retries cannot hide a leaked one.
func TestShutdownHTTPClosesIdleKeepAliveConnection(t *testing.T) {
	idle := make(chan struct{}, 1)
	api := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))
	api.Config.ConnState = func(_ net.Conn, state http.ConnState) {
		if state == http.StateIdle {
			select {
			case idle <- struct{}{}:
			default:
			}
		}
	}
	api.Start()
	t.Cleanup(api.Close)
	conn, err := net.DialTimeout("tcp", api.Listener.Addr().String(), 3*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	if err := conn.SetDeadline(time.Now().Add(5 * time.Second)); err != nil {
		t.Fatal(err)
	}
	if _, err := fmt.Fprintf(conn, "GET /healthz HTTP/1.1\r\nHost: %s\r\n\r\n", api.Listener.Addr()); err != nil {
		t.Fatal(err)
	}
	reader := bufio.NewReader(conn)
	response, err := http.ReadResponse(reader, &http.Request{Method: http.MethodGet})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := io.Copy(io.Discard, response.Body); err != nil {
		t.Fatal(err)
	}
	if err := response.Body.Close(); err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != http.StatusNoContent || response.Close {
		t.Fatalf("response=%d close=%v", response.StatusCode, response.Close)
	}
	awaitHTTPShutdownStep(t, "idle connection", idle)
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if err := shutdownHTTP(ctx, api.Config); err != nil {
		t.Fatal(err)
	}
	if err := conn.SetReadDeadline(time.Now().Add(3 * time.Second)); err != nil {
		t.Fatal(err)
	}
	if _, err := reader.ReadByte(); err != io.EOF {
		t.Fatalf("idle connection read=%v; want EOF, not timeout", err)
	}
	// A second shutdown is safe, and the API listener remains closed.
	if err := shutdownHTTP(ctx, api.Config); err != nil {
		t.Fatalf("repeat shutdown: %v", err)
	}
	unexpected, err := net.DialTimeout("tcp", api.Listener.Addr().String(), time.Second)
	if err == nil {
		unexpected.Close()
		t.Fatal("listener accepted a connection after shutdown")
	}
}
