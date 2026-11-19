// SPDX-License-Identifier: GPL-3.0-or-later
package relay

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/http/httptrace"
	"testing"
	"time"

	"github.com/ethereum/go-ethereum/p2p/enode"
)

// The HTTP transport must keep observing current readiness after a 503 on the
// same connection. Session transitions are local fixtures, not P2P handshakes.
func TestReadinessKeepAliveAcrossSessionTransitions(t *testing.T) {
	for _, observeOnly := range []bool{false, true} {
		name := "normal"
		if observeOnly {
			name = "observation"
		}
		t.Run(name, func(t *testing.T) {
			n := loadNode(t, 1, 0, 1)
			handler := n.Handler()
			if observeOnly {
				handler = n.ObservationHandler()
			}
			server := httptest.NewServer(handler)
			defer server.Close()
			transport := &http.Transport{Proxy: nil, MaxConnsPerHost: 1, MaxIdleConnsPerHost: 1}
			defer transport.CloseIdleConnections()
			client := &http.Client{Transport: transport, Timeout: 3 * time.Second}
			id := enode.ID{1}
			session := &peerSession{queue: make(chan outbound, 1), done: make(chan struct{})}
			before := n.Stats()
			requests := 0
			for _, phase := range []struct {
				name               string
				connected, receive bool
			}{
				{"disconnected", false, false}, {"connected_nonreceiving", true, false},
				{"receiving", true, true}, {"removed", false, false}, {"reconnected", true, true},
			} {
				n.mu.Lock()
				delete(n.peers, id)
				session.receiveTx = phase.receive
				if phase.connected {
					n.peers[id] = session
				}
				n.mu.Unlock()
				for _, path := range []string{"/readyz", "/healthz"} {
					req, err := http.NewRequest(http.MethodGet, server.URL+path, nil)
					if err != nil {
						t.Fatal(err)
					}
					reused := false
					req = req.WithContext(httptrace.WithClientTrace(req.Context(), &httptrace.ClientTrace{
						GotConn: func(info httptrace.GotConnInfo) { reused = info.Reused },
					}))
					response, err := client.Do(req)
					if err != nil {
						t.Fatal(err)
					}
					payload, readErr := io.ReadAll(response.Body)
					closeErr := response.Body.Close()
					if readErr != nil || closeErr != nil {
						t.Fatalf("body: %v / %v", readErr, closeErr)
					}
					if requests > 0 && !reused {
						t.Fatalf("%s %s: connection was not reused", phase.name, path)
					}
					requests++
					wantStatus, ready, connected := http.StatusOK, 0, 0
					if phase.connected {
						connected = 1
					}
					if phase.connected && phase.receive {
						ready = 1
					}
					if path == "/readyz" && ready == 0 {
						wantStatus = http.StatusServiceUnavailable
					}
					var health Health
					if err := json.Unmarshal(payload, &health); err != nil {
						t.Fatal(err)
					}
					if response.StatusCode != wantStatus || health.ConnectedPeers != connected || health.ReadyPeers != ready {
						t.Fatalf("%s %s: status=%d health=%+v", phase.name, path, response.StatusCode, health)
					}
					if response.Header.Get("Cache-Control") != "no-store" || response.Header.Get("Content-Type") != "application/json" {
						t.Fatalf("%s: observation headers changed", phase.name)
					}
				}
			}
			after := n.Stats()
			if before.Submit != after.Submit || before.HTTP != after.HTTP || n.writes.Load() != 0 || len(session.queue) != 0 {
				t.Fatal("HTTP observation produced submission or write activity")
			}
		})
	}
}
