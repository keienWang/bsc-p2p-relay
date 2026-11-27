// SPDX-License-Identifier: GPL-3.0-or-later
package main

import (
	"bufio"
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"sync/atomic"
	"testing"
	"time"

	"github.com/keienWang/bsc-p2p-relay/internal/relay"
)

// Exercise the live CLI lifecycle with an actual keep-alive client. Hand
// Serve an owned loopback socket so no other test can take a released port
// between address selection and startup.
func TestRunRelayCancellationReleasesObservationListener(t *testing.T) {
	listener, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	api := listener.Addr().String()

	cfg := relay.DefaultConfig()
	cfg.ListenAddr = "127.0.0.1:0"
	cfg.NoDiscovery = true
	ctx, cancel := context.WithCancel(context.Background())
	var restores atomic.Int32
	var runErr error
	done := make(chan struct{})
	go func() {
		defer close(done)
		runErr = runRelayWithHTTPListener(ctx, func() { restores.Add(1) }, cfg, api, true, defaultHTTPDrainTimeout,
			func(network, address string) (net.Listener, error) {
				if network != "tcp" || address != api {
					return nil, fmt.Errorf("unexpected observation bind %s %s", network, address)
				}
				return listener, nil
			})
	}()
	t.Cleanup(func() {
		cancel()
		select {
		case <-done:
		case <-time.After(6 * time.Second):
			t.Error("relay did not return after cleanup cancellation")
		}
	})

	conn, err := net.DialTimeout("tcp4", api, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	if err := conn.SetDeadline(time.Now().Add(5 * time.Second)); err != nil {
		t.Fatal(err)
	}
	reader := bufio.NewReader(conn)
	for range 2 {
		// A second response on the same socket proves it is a live
		// persistent connection, not just an accepted TCP handshake.
		if _, err := fmt.Fprintf(conn, "GET /healthz HTTP/1.1\r\nHost: %s\r\n\r\n", api); err != nil {
			t.Fatal(err)
		}
		response, err := http.ReadResponse(reader, &http.Request{Method: http.MethodGet})
		if err != nil {
			t.Fatal(err)
		}
		_, copyErr := io.Copy(io.Discard, response.Body)
		closeErr := response.Body.Close()
		if copyErr != nil || closeErr != nil || response.StatusCode != http.StatusOK || response.Close {
			t.Fatalf("health response was not reusable: status=%d close=%t copy=%v closeBody=%v",
				response.StatusCode, response.Close, copyErr, closeErr)
		}
	}
	if restores.Load() != 0 {
		t.Fatal("signal handling was restored before cancellation")
	}

	cancel()
	select {
	case <-done:
	case <-time.After(6 * time.Second):
		t.Fatal("relay did not return after cancellation")
	}
	if runErr != nil {
		t.Fatalf("running relay returned %v after cancellation", runErr)
	}
	if got := restores.Load(); got != 1 {
		t.Fatalf("signal restoration called %d times; want 1", got)
	}
	if err := conn.SetReadDeadline(time.Now().Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	if _, err := reader.ReadByte(); err != io.EOF {
		t.Fatalf("idle observation connection not closed cleanly after cancellation: %v", err)
	}
	reopened, err := net.Listen("tcp4", api)
	if err != nil {
		t.Fatalf("observation listener still occupied after runRelay returned: %v", err)
	}
	if err := reopened.Close(); err != nil {
		t.Fatal(err)
	}
}
