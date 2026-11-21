// SPDX-License-Identifier: GPL-3.0-or-later
package relay

import (
	"encoding/json"
	"math/big"
	"net/http"
	"net/http/httptest"
	"runtime"
	"strconv"
	"testing"
	"time"

	"github.com/ethereum/go-ethereum/p2p/enode"
)

// Readiness means negotiated receiveTx capability, not cache/queue admission.
// Synthetic sessions are never started and these fixtures send no messages.
func TestHealthReadinessCapabilityAndCapacity(t *testing.T) {
	n := loadNode(t, 1, 1, 1)
	txs, _ := loadFixtures(t, 1)
	if fresh, err := n.cache.add(txs[0], time.Now()); !fresh || err != nil {
		t.Fatalf("fixture: %v %v", fresh, err)
	}
	for _, p := range n.peers {
		p.queue <- outbound{}
	}
	before := n.Stats()
	for _, observationOnly := range []bool{false, true} {
		handler := n.Handler()
		if observationOnly {
			handler = n.ObservationHandler()
		}
		for _, ready := range []bool{false, true, false} {
			n.mu.Lock()
			id := enode.ID{1}
			p := n.peers[id]
			n.removePeerLocked(id, p)
			p.receiveTx = ready
			n.setPeerLocked(id, p)
			n.mu.Unlock()
			for _, path := range []string{"/healthz", "/readyz"} {
				w := httptest.NewRecorder()
				handler.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "http://localhost"+path, nil))
				want := http.StatusOK
				if path == "/readyz" && !ready {
					want = http.StatusServiceUnavailable
				}
				var h Health
				if err := json.Unmarshal(w.Body.Bytes(), &h); err != nil {
					t.Fatal(err)
				}
				readyCount := 0
				if ready {
					readyCount = 1
				}
				if w.Code != want || h.ReadyPeers != readyCount || h.ConnectedPeers != 1 || h.CachedTransactions != 1 || h.CachedBytes != int(txs[0].Size()) || h.WrittenMessages != 0 || h.WriteFailures != 0 || h.ConfirmationTracking {
					t.Fatalf("observation=%v ready=%v path=%s status=%d health=%+v", observationOnly, ready, path, w.Code, h)
				}
			}
		}
	}
	after := n.Stats()
	if before.Submit != after.Submit || before.HTTP != after.HTTP {
		t.Fatal("readiness polling changed counters")
	}
	for _, p := range n.peers {
		if len(p.queue) != 1 {
			t.Fatal("readiness polling drained queue")
		}
	}
}

func TestHealthTracksExactPeerSessionThroughReplacement(t *testing.T) {
	n := loadNode(t, 1, 0, 1)
	id, otherID := enode.ID{1}, enode.ID{2}
	first, replacement := &peerSession{receiveTx: true}, &peerSession{receiveTx: true}
	nonreceiving := &peerSession{}
	for _, step := range []struct {
		name             string
		id               enode.ID
		session          *peerSession
		remove           bool
		ready, connected int
	}{
		{"registered nonreceiving", otherID, nonreceiving, false, 0, 1},
		{"registered receiving", id, first, false, 1, 2},
		{"same id replacement", id, replacement, false, 1, 2},
		{"late old cleanup", id, first, true, 1, 2},
		{"removed nonreceiving", otherID, nonreceiving, true, 1, 1},
		{"replaced by nonreceiving", id, nonreceiving, false, 0, 1},
		{"late receiving cleanup", id, replacement, true, 0, 1},
		{"removed replacement", id, nonreceiving, true, 0, 0},
		{"repeated cleanup", id, nonreceiving, true, 0, 0},
	} {
		n.mu.Lock()
		if step.remove {
			n.removePeerLocked(step.id, step.session)
		} else {
			n.setPeerLocked(step.id, step.session)
		}
		n.mu.Unlock()
		got := n.Health()
		if got.ReadyPeers != step.ready || got.ConnectedPeers != step.connected {
			t.Fatalf("%s: got %d/%d ready/connected; want %d/%d", step.name, got.ReadyPeers, got.ConnectedPeers, step.ready, step.connected)
		}
	}
}

// Unlike Stats, Health takes peer/cache locks and may prune expired entries.
// Measure stable non-expiring caches separately from HTTP recorder overhead.
func BenchmarkHealthObservation(b *testing.B) {
	for _, peers := range []int{0, 20, 200} {
		for _, occupancy := range []int{0, 1024} {
			b.Run("sessions_"+strconv.Itoa(peers)+"/cache_"+strconv.Itoa(occupancy), func(b *testing.B) {
				n := loadNode(b, 1024, peers, 1)
				txs, _ := loadFixtures(b, occupancy)
				now := time.Now()
				for _, tx := range txs {
					if fresh, err := n.cache.add(tx, now); !fresh || err != nil {
						b.Fatalf("fixture: %v %v", fresh, err)
					}
				}
				b.Run("snapshot_serial", func(b *testing.B) {
					var last Health
					b.ReportAllocs()
					b.ResetTimer()
					for i := 0; i < b.N; i++ {
						last = n.Health()
					}
					runtime.KeepAlive(last)
				})
				b.Run("snapshot_parallel", func(b *testing.B) {
					b.ReportAllocs()
					b.ResetTimer()
					b.RunParallel(func(pb *testing.PB) {
						var last Health
						for pb.Next() {
							last = n.Health()
						}
						runtime.KeepAlive(last)
					})
				})
				handler := n.ObservationHandler()
				request := httptest.NewRequest(http.MethodGet, "http://localhost/readyz", nil)
				want := http.StatusOK
				if peers == 0 {
					want = http.StatusServiceUnavailable
				}
				b.Run("readiness_handler_recorder", func(b *testing.B) {
					b.ReportAllocs()
					b.ResetTimer()
					for i := 0; i < b.N; i++ {
						w := httptest.NewRecorder()
						handler.ServeHTTP(w, request)
						if w.Code != want || w.Body.Len() == 0 {
							b.Fatal("invalid readiness response")
						}
					}
				})
			})
		}
	}
}

func TestHealthChainIdentityAndAllocations(t *testing.T) {
	for _, name := range []string{"mainnet", "testnet"} {
		t.Run(name, func(t *testing.T) {
			n := loadNode(t, 1, 0, 1)
			n.cfg.Network = name
			chain, err := network(name)
			if err != nil {
				t.Fatal(err)
			}
			n.chain = chain
			want := n.chain.config.ChainID.String()
			var last Health
			allocations := testing.AllocsPerRun(1000, func() { last = n.Health() })
			if last.ChainID != want || last.Network != name {
				t.Fatalf("wrong network identity: %+v", last)
			}
			if allocations != 0 {
				t.Fatalf("Health allocations=%g; want zero", allocations)
			}
			runtime.KeepAlive(last)
		})
	}
}

func TestHealthReflectsReplacementChainView(t *testing.T) {
	n := loadNode(t, 1, 0, 1)
	original := n.Health().ChainID
	cfg := *n.chain.config
	cfg.ChainID = big.NewInt(13371338)
	n.chain = newChainView(&cfg, n.chain.genesis)
	if h := n.Health(); h.ChainID != "13371338" || h.ChainID == original {
		t.Fatalf("replacement view has stale identity: %+v", h)
	}
}
