// SPDX-License-Identifier: GPL-3.0-or-later
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/keienWang/bsc-p2p-relay/internal/relay"
)

// An occupied HTTP port is an ordinary startup failure, not a successfully
// started API. The previously started P2P listener must also be released.
// This test changes the default logger and must not run in parallel.
func TestRunRelayAPIBindFailureDoesNotAnnounceStartup(t *testing.T) {
	api, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer api.Close()
	probe, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	p2pAddress := probe.Addr().String()
	if err := probe.Close(); err != nil {
		t.Fatal(err)
	}
	cfg := relay.DefaultConfig()
	cfg.NoDiscovery = true
	cfg.ListenAddr = p2pAddress
	var logs bytes.Buffer
	previous := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&logs, nil)))
	defer slog.SetDefault(previous)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	restored := false
	err = runRelay(ctx, func() { restored = true }, cfg, api.Addr().String(), true)
	var networkError *net.OpError
	if !errors.As(err, &networkError) || networkError.Op != "listen" || networkError.Addr == nil || networkError.Addr.String() != api.Addr().String() {
		t.Fatalf("want API listen failure, got %v", err)
	}
	if !errors.Is(err, syscall.EADDRINUSE) || !strings.HasPrefix(err.Error(), "bind HTTP API: ") {
		t.Fatalf("want actionable HTTP bind error with preserved cause, got %v", err)
	}
	if strings.Contains(logs.String(), "experimental relay started") {
		t.Fatalf("announced startup after API bind failure: %s", logs.String())
	}
	if restored {
		t.Fatal("bind failure incorrectly followed cancellation branch")
	}
	reopened, err := net.Listen("tcp4", p2pAddress)
	if err != nil {
		t.Fatalf("P2P listener retained after API bind failure: %v", err)
	}
	if err := reopened.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestRunRelayIdentifiesP2PBindFailure(t *testing.T) {
	listener, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	cfg := relay.DefaultConfig()
	cfg.NoDiscovery = true
	cfg.ListenAddr = listener.Addr().String()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	err = runRelay(ctx, func() { t.Error("unexpected cancellation callback") }, cfg, "127.0.0.1:0", true)
	var networkError *net.OpError
	if !errors.Is(err, syscall.EADDRINUSE) || !errors.As(err, &networkError) || networkError.Op != "listen" || networkError.Addr == nil || networkError.Addr.String() != cfg.ListenAddr {
		t.Fatalf("want underlying occupied P2P listen error, got %v", err)
	}
	if !strings.HasPrefix(err.Error(), "start P2P node: ") {
		t.Fatalf("want actionable P2P startup error, got %v", err)
	}
}

// The selected ephemeral port, not the configured :0 placeholder, must be
// usable from the startup observation. No polling/port reservation is needed.
type startupLogWriter func([]byte) (int, error)

func (w startupLogWriter) Write(p []byte) (int, error) { return w(p) }

func TestRunRelayLogsBoundEphemeralAPI(t *testing.T) {
	logs := make(chan []byte, 8)
	previous := slog.Default()
	slog.SetDefault(slog.New(slog.NewJSONHandler(startupLogWriter(func(p []byte) (int, error) {
		select {
		case logs <- bytes.Clone(p):
		default:
		}
		return len(p), nil
	}), nil)))
	t.Cleanup(func() { slog.SetDefault(previous) })
	cfg := relay.DefaultConfig()
	cfg.NoDiscovery = true
	cfg.ListenAddr = "127.0.0.1:0"
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- runRelay(ctx, func() {}, cfg, "127.0.0.1:0", true) }()
	t.Cleanup(func() {
		cancel()
		select {
		case err := <-done:
			if err != nil {
				t.Errorf("relay exit: %v", err)
			}
		case <-time.After(6 * time.Second):
			t.Error("relay did not stop")
		}
	})
	var record map[string]any
	select {
	case payload := <-logs:
		if err := json.Unmarshal(payload, &record); err != nil {
			t.Fatal(err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("startup not logged")
	}
	api, ok := record["api"].(string)
	if !ok {
		t.Fatalf("missing API address: %v", record)
	}
	host, port, err := net.SplitHostPort(api)
	if err != nil || host != "127.0.0.1" || port == "0" || port == "" {
		t.Fatalf("invalid bound API address %q: %v", api, err)
	}
	client := &http.Client{Transport: &http.Transport{DisableKeepAlives: true}, Timeout: 2 * time.Second}
	response, err := client.Get("http://" + api + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("logged endpoint health: %s", response.Status)
	}
}
