// SPDX-License-Identifier: GPL-3.0-or-later
package relay

import (
	"sync"
	"testing"
	"time"

	"github.com/ethereum/go-ethereum/common"
)

func TestStatsLedgerUniqueScenarios(t *testing.T) {
	// Reuse the independent per-hash and validated HTTP ledger without changing
	// its report schema or making a second performance/soak measurement.
	newHarness := func(t *testing.T, count, bytes int, ttl time.Duration) (*uniqueHarness, ledgerSnapshot) {
		h := newUniqueHarness(t, count, bytes, ttl, make(map[string]any), t.Name())
		return h, ledgerHTTPSnapshot(t, h)
	}
	t.Run("count_full_and_cached_retry", func(t *testing.T) {
		plan := ledgerExpectation{ledgerVector{ledgerNew: 2, ledgerCached: 1, ledgerCacheFull: 1}, 4, 0}
		h, before := newHarness(t, 2, MaxRawTransaction, time.Minute)
		h.peer(t, 1, "fast", nil)
		first := h.gen.next(t, 0)
		h.mustSubmit(t, first, "first", false, uniqueAccept(false, 1, 1))
		h.mustSubmit(t, h.gen.next(t, 0), "second", false, uniqueAccept(false, 1, 1))
		h.mustSubmit(t, h.gen.next(t, 0), "count_full", false, uniqueReject(429, ErrCacheFull))
		h.mustSubmit(t, first, "cached_at_capacity", true, uniqueAccept(true, 0, 0))
		h.awaitWrites(t, 2)
		ledgerCheckUnique(t, h, before, plan, 3, 1)
	})
	t.Run("bytes_before_count", func(t *testing.T) {
		plan := ledgerExpectation{ledgerVector{ledgerNew: 1, ledgerCached: 1, ledgerCacheFull: 1}, 3, 0}
		h, before := newHarness(t, 16, MaxRawTransaction, time.Minute)
		h.peer(t, 1, "fast", nil)
		first := h.gen.next(t, 70*1024)
		h.mustSubmit(t, first, "first", false, uniqueAccept(false, 1, 1))
		h.mustSubmit(t, h.gen.next(t, 70*1024), "bytes_full", false, uniqueReject(429, ErrCacheFull))
		h.mustSubmit(t, first, "cached", true, uniqueAccept(true, 0, 0))
		h.awaitWrites(t, 1)
		ledgerCheckUnique(t, h, before, plan, 2, 1)
	})
	t.Run("queue_rollback_partial_and_retry", func(t *testing.T) {
		// 67 different hashes; 5 old-hash attempts; 67 new lifetimes, 3 cached
		// returns and 2 queue refusals. 70 queue items do not define the 72 calls.
		plan := ledgerExpectation{ledgerVector{ledgerNew: 67, ledgerCached: 3, ledgerQueue: 2}, 72, 0}
		h, before := newHarness(t, 256, 16*MaxRawTransaction, time.Minute)
		gate := make(chan struct{})
		h.peer(t, 1, "blocked", gate)
		first := h.gen.next(t, 0)
		h.mustSubmit(t, first, "blocked_inflight", false, uniqueAccept(false, 1, 1))
		uniqueWait(t, "blocked MsgPipe write", func() bool { return h.stats.get("write_attempts") == 1 })
		for i := 0; i < 64; i++ {
			h.mustSubmit(t, h.gen.next(t, 0), "fill_queue", false, uniqueAccept(false, 1, 1))
		}
		rejected := h.gen.next(t, 0)
		h.mustSubmit(t, rejected, "fresh_rollback", false, uniqueReject(429, ErrQueuesFull))
		h.peer(t, 2, "fast", nil)
		h.mustSubmit(t, first, "cached_new_peer", true, uniqueAccept(true, 1, 1))
		partial := h.gen.next(t, 0)
		h.mustSubmit(t, partial, "partial_fanout", false, uniqueAccept(false, 1, 1))
		h.mustSubmit(t, partial, "cached_uncovered_full_peer", true, uniqueReject(429, ErrQueuesFull))
		close(gate)
		h.awaitWrites(t, 67)
		h.mustSubmit(t, partial, "cached_remaining_peer", true, uniqueAccept(true, 1, 1))
		h.awaitWrites(t, 68)
		h.mustSubmit(t, partial, "cached_all_covered", true, uniqueAccept(true, 0, 0))
		h.mustSubmit(t, rejected, "old_hash_fresh_after_rollback", true, uniqueAccept(false, 2, 2))
		h.awaitWrites(t, 70)
		ledgerCheckUnique(t, h, before, plan, 67, 5)
	})
	t.Run("same_hash_after_short_ttl", func(t *testing.T) {
		plan := ledgerExpectation{ledgerVector{ledgerNew: 2, ledgerCached: 1}, 3, 0}
		const ttl = time.Second
		h, before := newHarness(t, 4, MaxRawTransaction, ttl)
		h.peer(t, 1, "fast", nil)
		first := h.gen.next(t, 0)
		h.mustSubmit(t, first, "first_lifetime", false, uniqueAccept(false, 1, 1))
		h.mustSubmit(t, first, "live_cached_retry", true, uniqueAccept(true, 0, 0))
		h.awaitWrites(t, 1)
		// The completed request is after insertion. Waiting a full configured
		// TTL from here proves expiry without reading the internal cache clock.
		time.Sleep(ttl + time.Millisecond)
		h.mustSubmit(t, first, "old_hash_new_lifetime", true, uniqueAccept(false, 1, 1))
		h.awaitWrites(t, 2)
		ledgerCheckUnique(t, h, before, plan, 1, 2)
	})
	t.Run("peer_churn_and_no_peers", func(t *testing.T) {
		plan := ledgerExpectation{ledgerVector{ledgerNew: 1, ledgerCached: 1, ledgerNoPeers: 1}, 3, 0}
		h, before := newHarness(t, 4, MaxRawTransaction, time.Minute)
		p := h.peer(t, 1, "old", nil)
		first := h.gen.next(t, 0)
		h.mustSubmit(t, first, "before_churn", false, uniqueAccept(false, 1, 1))
		h.awaitWrites(t, 1)
		p.close(t, h.n)
		h.mustSubmit(t, h.gen.next(t, 0), "no_peers", false, uniqueReject(503, ErrNoPeers))
		h.peer(t, 2, "replacement", nil)
		h.mustSubmit(t, first, "cached_replacement", true, uniqueAccept(true, 1, 1))
		h.awaitWrites(t, 2)
		ledgerCheckUnique(t, h, before, plan, 2, 1)
	})
	for _, kind := range []string{"closing", "sent_bound", "invalid_internal_ttl_fault"} {
		t.Run(kind, func(t *testing.T) {
			plan := ledgerExpectation{ledgerVector{ledgerQueue: 1}, 1, 0}
			want := ErrQueuesFull
			if kind == "invalid_internal_ttl_fault" {
				plan.counters = ledgerVector{ledgerExpired: 1}
				want = ErrAdmissionExpired
			}
			h, before := newHarness(t, 1, MaxRawTransaction, time.Minute)
			// Queue/closing/sent-map fixtures deliberately exercise sessions
			// which cannot enqueue. No fake successful transport is reported.
			p := addFakePeer(h.n, true, 64)
			switch kind {
			case "closing":
				close(p.done)
			case "sent_bound":
				p.sent = map[common.Hash]time.Time{{1}: time.Now().Add(time.Hour)}
			case "invalid_internal_ttl_fault":
				// New rejects this TTL. This is branch fault injection, NOT an
				// assertion that normal accepted configuration reaches expiry.
				h.n.cache = newStore(1, MaxRawTransaction, -time.Minute)
			}
			h.mustSubmit(t, h.gen.next(t, 0), kind, false, uniqueReject(429, want))
			ledgerCheckUnique(t, h, before, plan, 1, 0)
		})
	}
	t.Run("joined_concurrent_unique", func(t *testing.T) {
		plan := ledgerExpectation{ledgerVector{ledgerNew: 2, ledgerCacheFull: 2}, 4, 0}
		h, before := newHarness(t, 2, MaxRawTransaction, time.Minute)
		h.peer(t, 1, "fast_a", nil)
		h.peer(t, 2, "fast_b", nil)
		contract := uniqueAccept(false, 2, 2)
		contract.rejections = map[int]string{429: ErrCacheFull.Error()}
		start := make(chan struct{})
		var joined sync.WaitGroup
		for i := 0; i < 4; i++ {
			f := h.gen.next(t, 0)
			joined.Add(1)
			go func() {
				defer joined.Done()
				<-start
				if _, _, err := h.submit(f, "concurrent", false, contract); err != nil {
					t.Error(err)
				}
			}()
		}
		close(start)
		joined.Wait()
		h.awaitWrites(t, 4)
		ledgerCheckUnique(t, h, before, plan, 4, 0)
	})
}
