// SPDX-License-Identifier: GPL-3.0-or-later
package main

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func awaitHTTPShutdownStep[T any](t *testing.T, label string, step <-chan T) T {
	t.Helper()
	select {
	case result := <-step:
		return result
	case <-time.After(5 * time.Second):
		t.Fatalf("timed out waiting for %s", label)
		var zero T
		return zero
	}
}

func TestShutdownHTTPDrainsActiveRequest(t *testing.T) {
	started, release := make(chan struct{}), make(chan struct{})
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		close(started)
		select {
		case <-release:
			w.WriteHeader(http.StatusNoContent)
		case <-r.Context().Done():
		}
	}))
	t.Cleanup(api.Close)
	draining := make(chan struct{})
	api.Config.RegisterOnShutdown(func() { close(draining) })

	type outcome struct {
		status int
		err    error
	}
	reply := make(chan outcome, 1)
	go func() {
		resp, err := api.Client().Get(api.URL)
		if err != nil {
			reply <- outcome{err: err}
			return
		}
		_ = resp.Body.Close()
		reply <- outcome{status: resp.StatusCode}
	}()
	awaitHTTPShutdownStep(t, "active request", started)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	result := make(chan error, 1)
	go func() { result <- shutdownHTTP(ctx, api.Config) }()
	awaitHTTPShutdownStep(t, "graceful shutdown start", draining)
	close(release)

	got := awaitHTTPShutdownStep(t, "successful drained response", reply)
	if got.err != nil || got.status != http.StatusNoContent {
		t.Fatalf("drained response = %+v, want HTTP 204", got)
	}
	if err := awaitHTTPShutdownStep(t, "graceful shutdown return", result); err != nil {
		t.Fatalf("graceful shutdown = %v, want nil", err)
	}
}

func TestShutdownHTTPClosesActiveRequestWhenDrainCanceled(t *testing.T) {
	started, stopped := make(chan struct{}), make(chan struct{})
	api := httptest.NewServer(http.HandlerFunc(func(_ http.ResponseWriter, r *http.Request) {
		close(started)
		<-r.Context().Done()
		close(stopped)
	}))
	t.Cleanup(api.Close)
	reply := make(chan error, 1)
	go func() {
		resp, err := api.Client().Get(api.URL)
		if resp != nil {
			_ = resp.Body.Close()
		}
		reply <- err
	}()
	awaitHTTPShutdownStep(t, "active request", started)

	ctx, cancel := context.WithCancel(context.Background())
	cancel() // Expire the drain without depending on a wall-clock race.
	if err := shutdownHTTP(ctx, api.Config); !errors.Is(err, context.Canceled) {
		t.Fatalf("expired drain = %v, want context.Canceled", err)
	}
	awaitHTTPShutdownStep(t, "active request context cancellation", stopped)
	if err := awaitHTTPShutdownStep(t, "closed client connection", reply); err == nil {
		t.Fatal("client unexpectedly received an HTTP response after force close")
	}
}
