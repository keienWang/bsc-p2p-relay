// SPDX-License-Identifier: GPL-3.0-or-later
package main

import (
	"context"
	"net"
	"net/http"
	"sync/atomic"
	"testing"
	"time"

	"github.com/keienWang/bsc-p2p-relay/internal/relay"
)

// Exercise the live CLI lifecycle, rather than a synthetic context branch or
// Node.Close in isolation. All listeners and client calls stay on loopback.
func TestRunRelayCancellationReleasesObservationListener(t *testing.T) {
	probe, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	api := probe.Addr().String()
	if err := probe.Close(); err != nil {
		t.Fatal(err)
	}

	cfg := relay.DefaultConfig()
	cfg.ListenAddr = "127.0.0.1:0"
	cfg.NoDiscovery = true
	ctx, cancel := context.WithCancel(context.Background())
	var restores atomic.Int32
	var runErr error
	done := make(chan struct{})
	go func() {
		defer close(done)
		runErr = runRelay(ctx, func() { restores.Add(1) }, cfg, api, true)
	}()
	t.Cleanup(func() {
		cancel()
		select {
		case <-done:
		case <-time.After(6 * time.Second):
			t.Error("relay did not return after cleanup cancellation")
		}
	})

	client := &http.Client{
		Transport:     &http.Transport{DisableKeepAlives: true},
		Timeout:       250 * time.Millisecond,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
	startup := time.NewTimer(5 * time.Second)
	defer startup.Stop()
	poll := time.NewTicker(10 * time.Millisecond)
	defer poll.Stop()
	for {
		response, err := client.Get("http://" + api + "/healthz")
		if err == nil {
			_ = response.Body.Close()
			if response.StatusCode != http.StatusOK {
				t.Fatalf("observation health returned HTTP %d", response.StatusCode)
			}
			break
		}
		select {
		case <-done:
			t.Fatalf("relay returned before observation startup: %v", runErr)
		case <-startup.C:
			t.Fatalf("observation listener did not start: %v", err)
		case <-poll.C:
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
	reopened, err := net.Listen("tcp4", api)
	if err != nil {
		t.Fatalf("observation listener still occupied after runRelay returned: %v", err)
	}
	if err := reopened.Close(); err != nil {
		t.Fatal(err)
	}
}
