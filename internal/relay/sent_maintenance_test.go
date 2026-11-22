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

func TestSentMaintenanceCeilingNeverDropsLiveRecords(t *testing.T) {
	p := &peerSession{}
	now := time.Unix(1700000000, 0)
	early, late := common.Hash{1}, common.Hash{2}
	later := now.Add(time.Minute)
	p.recordSent(early, now)
	p.recordSent(late, later)
	p.pruneSent(now)
	if len(p.sent) != 1 || !p.sent[late].Equal(later) {
		t.Fatal("mixed deadlines lost a live record")
	}
	p.recordSent(early, later)
	p.pruneSent(later)
	if len(p.sent) != 0 || !p.sentExpiryFloor.IsZero() || !p.sentExpiryCeil.IsZero() {
		t.Fatal("all-due cohort was not cleared at deadline equality")
	}
	// Deleting the only latest record can leave a stale conservative ceiling.
	// The remaining earlier record must still expire via the ordinary path.
	p.recordSent(early, now)
	p.recordSent(late, later)
	delete(p.sent, late)
	p.pruneSent(now)
	if len(p.sent) != 0 || !p.sentExpiryCeil.IsZero() {
		t.Fatal("a stale late ceiling prevented exact expiration or empty reset")
	}
	p.recordSent(early, later)
	p.pruneSent(now)
	if !p.sent[early].Equal(later) {
		t.Fatal("refill lost a new future deadline")
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

// BenchmarkSentMaintenanceDue times only the prune pass; run with a fixed
// iteration count, e.g. -benchtime=30x, since refills are not timed.
func BenchmarkSentMaintenanceDue(b *testing.B) {
	const records = 1024
	now := time.Unix(1700000000, 0)
	later := now.Add(time.Minute)
	for _, peers := range []int{20, 200} {
		for _, due := range []int{records, records / 8} {
			name := "sessions_" + strconv.Itoa(peers) + "/due_" + strconv.Itoa(due)
			b.Run(name, func(b *testing.B) {
				b.StopTimer()
				n := loadNode(b, records, peers, 1)
				for _, p := range n.peers {
					p.sent = make(map[common.Hash]time.Time, records)
				}
				b.ReportAllocs()
				b.ResetTimer()
				for range b.N {
					for _, p := range n.peers {
						for i := range records {
							deadline := now
							if i >= due {
								deadline = later
							}
							p.recordSent(common.Hash{byte(i), byte(i >> 8)}, deadline)
						}
					}
					b.StartTimer()
					n.prune(now)
					b.StopTimer()
					for _, p := range n.peers {
						if len(p.sent) != records-due {
							b.Fatalf("prune kept %d records; want %d", len(p.sent), records-due)
						}
					}
				}
				b.ReportMetric(float64(peers*due), "expired/op")
			})
		}
	}
}
