// SPDX-License-Identifier: GPL-3.0-or-later
package relay

import (
	"strconv"
	"testing"
	"time"

	"github.com/ethereum/go-ethereum/common"
)

func TestSentMaintenancePreservesExactDeadlines(t *testing.T) {
	p := &peerSession{}
	now := time.Unix(1700000000, 0)
	first, second, third := common.Hash{1}, common.Hash{2}, common.Hash{3}
	p.recordSent(first, now.Add(2*time.Second))
	p.recordSent(second, now.Add(time.Second))
	p.pruneSent(now)
	if len(p.sent) != 2 || !p.sentNextSweep.Equal(now.Add(time.Second)) {
		t.Fatal("no-expiry pass removed entries or failed to advance the sweep schedule")
	}
	p.pruneSent(now.Add(time.Second))
	if _, ok := p.sent[second]; ok || len(p.sent) != 1 || !p.sentExpiryFloor.Equal(now.Add(2*time.Second)) {
		t.Fatal("exact-deadline prune lost a live entry or failed to advance the floor")
	}
	midpoint := now.Add(1500 * time.Millisecond)
	p.recordSent(third, midpoint)
	p.pruneSent(midpoint)
	if _, ok := p.sent[third]; ok || len(p.sent) != 1 {
		t.Fatal("earlier inserted deadline was missed")
	}
	// Opportunistic removal may leave a stale early bound. It must not hide
	// later expiration or be inherited by a refill after the map is empty.
	delete(p.sent, first)
	refill := now.Add(time.Minute)
	p.recordSent(second, refill)
	p.pruneSent(now.Add(2 * time.Second))
	if len(p.sent) != 1 || !p.sentExpiryFloor.Equal(refill) {
		t.Fatal("refill inherited the expired bound")
	}
	p.pruneSent(refill)
	if len(p.sent) != 0 {
		t.Fatal("idle pass did not reclaim the final deadline")
	}
}

// BenchmarkSentMaintenance isolates a periodic pass over stable per-peer
// metadata. There are no sockets, messages, or transaction fixtures.
func BenchmarkSentMaintenance(b *testing.B) {
	for _, peers := range []int{20, 200} {
		b.Run("sessions_"+strconv.Itoa(peers), func(b *testing.B) {
			n := loadNode(b, 1024, peers, 1)
			now := time.Unix(1700000000, 0)
			deadline := now.Add(time.Minute)
			for _, p := range n.peers {
				p.sent = make(map[common.Hash]time.Time, 1024)
				for i := range 1024 {
					p.recordSent(common.Hash{byte(i), byte(i >> 8)}, deadline)
				}
			}
			n.prune(now)
			b.ReportAllocs()
			b.ResetTimer()
			for range b.N {
				n.prune(now)
			}
		})
	}
}
