// SPDX-License-Identifier: GPL-3.0-or-later
package main

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/keienWang/bsc-p2p-relay/internal/relay"
)

type failNextAcceptListener struct {
	net.Listener
	accepted bool // Serve is the only Accept caller.
	failure  <-chan struct{}
	err      error
}

func (l *failNextAcceptListener) Accept() (net.Conn, error) {
	if !l.accepted {
		l.accepted = true
		return l.Listener.Accept()
	}
	<-l.failure
	return nil, l.err
}

func TestRunRelayClosesHTTPConnectionsOnServeFailure(t *testing.T) {
	api, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer api.Close()
	failure := make(chan struct{})
	defer func() {
		select {
		case <-failure:
		default:
			close(failure)
		}
	}()
	acceptErr := errors.New("test terminal accept error")
	cfg := relay.DefaultConfig()
	cfg.ListenAddr = "127.0.0.1:0"
	cfg.NoDiscovery = true
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	result := make(chan error, 1)
	go func() {
		result <- runRelayWithHTTPListener(ctx, func() {}, cfg, api.Addr().String(), true,
			func(_, _ string) (net.Listener, error) {
				return &failNextAcceptListener{Listener: api, failure: failure, err: acceptErr}, nil
			})
	}()
	conn, err := net.DialTimeout("tcp4", api.Addr().String(), time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	if err := conn.SetDeadline(time.Now().Add(3 * time.Second)); err != nil {
		t.Fatal(err)
	}
	if _, err := fmt.Fprintf(conn, "GET /healthz HTTP/1.1\r\nHost: %s\r\n\r\n", api.Addr()); err != nil {
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
	if response.StatusCode != http.StatusOK || response.Close {
		t.Fatalf("pre-failure GET = %d close=%v; want a healthy keep-alive socket", response.StatusCode, response.Close)
	}

	close(failure)
	err = awaitHTTPShutdownStep(t, "terminal serve error", result)
	if !errors.Is(err, acceptErr) || !strings.HasPrefix(err.Error(), "serve HTTP API: ") {
		t.Fatalf("runRelay error = %v; want original Accept error with HTTP stage", err)
	}
	// Serve itself closes the listening socket, not sockets it already
	// accepted. A retrying HTTP client could hide that distinction.
	if _, err := reader.ReadByte(); !errors.Is(err, io.EOF) {
		t.Fatalf("accepted connection read = %v; want EOF after Serve error, not a timeout", err)
	}
}
