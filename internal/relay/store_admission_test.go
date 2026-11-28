// SPDX-License-Identifier: GPL-3.0-or-later
package relay

import (
	"errors"
	"testing"
	"time"

	"github.com/ethereum/go-ethereum/core/types"
)

// Keep the old two-result signature for cache-only tests and paired baseline
// benchmarks that intentionally do not consume an admission deadline.
func (s *store) add(tx *types.Transaction, now time.Time) (bool, error) {
	fresh, _, err := s.addWithExpiry(tx, now)
	return fresh, err
}

func TestStoreAdmissionReturnsExactLifetime(t *testing.T) {
	txs, _ := loadFixtures(t, 2)
	ttl := time.Minute
	s := newStore(1, MaxRawTransaction, ttl)
	now := time.Unix(1700000000, 0)
	deadline := now.Add(ttl)
	fresh, expires, err := s.addWithExpiry(txs[0], now)
	if !fresh || !expires.Equal(deadline) || err != nil {
		t.Fatalf("new lifetime: %v %v %v", fresh, expires, err)
	}
	fresh, expires, err = s.addWithExpiry(txs[0], deadline.Add(-time.Nanosecond))
	if fresh || !expires.Equal(deadline) || err != nil {
		t.Fatalf("duplicate changed deadline: %v %v %v", fresh, expires, err)
	}
	fresh, expires, err = s.addWithExpiry(txs[1], deadline.Add(-time.Nanosecond))
	if fresh || !expires.IsZero() || !errors.Is(err, ErrCacheFull) {
		t.Fatalf("rejection returned a lifetime: %v %v %v", fresh, expires, err)
	}
	fresh, expires, err = s.addWithExpiry(txs[0], deadline)
	if !fresh || !expires.Equal(deadline.Add(ttl)) || err != nil {
		t.Fatalf("expired hash did not get a new lifetime: %v %v %v", fresh, expires, err)
	}
	assertStoreIndex(t, s)
}

// Both variants reuse one cached transaction and consume its original
// deadline. The second variant measures the formerly separate deadline read.
func BenchmarkCachedAdmissionDeadline(b *testing.B) {
	txs, _ := loadFixtures(b, 1)
	tx := txs[0]
	hash := tx.Hash()
	now := time.Unix(1700000000, 0)
	for _, separate := range []bool{false, true} {
		name := "same_lock"
		if separate {
			name = "separate_lookup"
		}
		b.Run(name, func(b *testing.B) {
			s := newStore(1, MaxRawTransaction, time.Minute)
			_, deadline, err := s.addWithExpiry(tx, now)
			if err != nil {
				b.Fatal(err)
			}
			b.ReportAllocs()
			b.ResetTimer()
			for i := 0; i < b.N; i++ {
				fresh, expires, err := s.addWithExpiry(tx, now)
				if separate {
					expires = s.expiry(hash)
				}
				if fresh || err != nil || expires != deadline {
					b.Fatal("duplicate did not keep the original deadline")
				}
			}
		})
	}
}
