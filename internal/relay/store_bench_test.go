// SPDX-License-Identifier: GPL-3.0-or-later
package relay

import (
	"bytes"
	"errors"
	"math/big"
	"testing"
	"time"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/crypto"
)

// BenchmarkStoreCache uses only the pre-index store API so the exact same file
// can be applied to the scan baseline for paired runs. This isolates cache work,
// not HTTP/validation, peers, transport or sustained new-hash admission capacity.
func BenchmarkStoreCache(b *testing.B) {
	cfg := DefaultConfig()
	if cfg.CacheCount != 1024 || cfg.CacheBytes != 16<<20 || cfg.CacheTTL != 120*time.Second {
		b.Fatal("benchmark requires documented default cache limits")
	}
	txs, _ := loadFixtures(b, cfg.CacheCount+1)
	for _, tx := range txs {
		_ = tx.Hash()
		_ = tx.Size()
	}
	// All prepared cases reuse these signed objects and their cached hashes/sizes.
	// Signing and warming are excluded, and rejected_absent is the SAME absent
	// hash on every iteration, not a stream of previously unseen transactions.
	for _, tc := range []struct {
		name      string
		occupancy int
		rejected  bool
		rollback  bool
	}{
		{"near_900_duplicate_prepared", 900, false, false},
		{"full_1024_duplicate_prepared", 1024, false, false},
		{"full_1024_rejected_absent_prepared", 1024, true, false},
		{"near_900_add_remove_prepared", 900, false, true},
	} {
		b.Run(tc.name, func(b *testing.B) {
			s := newStore(cfg.CacheCount, cfg.CacheBytes, cfg.CacheTTL)
			now := time.Unix(1700000000, 0)
			for _, tx := range txs[:tc.occupancy] {
				if _, err := s.add(tx, now); err != nil {
					b.Fatal(err)
				}
			}
			tx := txs[tc.occupancy-1]
			if tc.rejected || tc.rollback {
				tx = txs[cfg.CacheCount]
			}
			b.ReportAllocs()
			b.ResetTimer()
			for i := 0; i < b.N; i++ {
				fresh, err := s.add(tx, now)
				if tc.rejected {
					if fresh || !errors.Is(err, ErrCacheFull) {
						b.Fatalf("rejected add: %v %v", fresh, err)
					}
				} else if tc.rollback {
					if !fresh || err != nil {
						b.Fatalf("rollback add: %v %v", fresh, err)
					}
					s.remove(tx.Hash()) // Timed, synthetic queue-admission rollback.
				} else if fresh || err != nil {
					b.Fatalf("duplicate add: %v %v", fresh, err)
				}
			}
		})
	}
	b.Run("expiry_cohort_1024_refill_and_prune_prepared", func(b *testing.B) {
		s := newStore(cfg.CacheCount, cfg.CacheBytes, cfg.CacheTTL)
		now := time.Unix(1700000000, 0)
		b.ReportAllocs()
		b.ResetTimer()
		for i := 0; i < b.N; i++ {
			for _, tx := range txs[:cfg.CacheCount] {
				if fresh, err := s.add(tx, now); !fresh || err != nil {
					b.Fatalf("refill: %v %v", fresh, err)
				}
			}
			now = now.Add(cfg.CacheTTL)
			s.prune(now)
		}
		b.ReportMetric(float64(cfg.CacheCount), "entries/op")
	})
	b.Run("full_1024_rejected_unique_signed_in_loop", func(b *testing.B) {
		s := newStore(cfg.CacheCount, cfg.CacheBytes, cfg.CacheTTL)
		now := time.Unix(1700000000, 0)
		for _, tx := range txs[:cfg.CacheCount] {
			if _, err := s.add(tx, now); err != nil {
				b.Fatal(err)
			}
		}
		key, err := crypto.ToECDSA(bytes.Repeat([]byte{2}, 32))
		if err != nil {
			b.Fatal(err)
		}
		signer := types.LatestSignerForChainID(big.NewInt(56))
		b.ReportAllocs()
		b.ResetTimer()
		for i := 0; i < b.N; i++ {
			// A distinct nonce produces a genuinely new signed hash each time.
			// Construction, signing, hash/size calculation AND rejected admission
			// are timed. This is deliberately not a cache-only comparison.
			tx := types.NewTransaction(uint64(len(txs))+uint64(i), common.Address{19: 1}, big.NewInt(0), 21000, big.NewInt(1), nil)
			signed, err := types.SignTx(tx, signer, key)
			if err != nil {
				b.Fatal(err)
			}
			if fresh, err := s.add(signed, now); fresh || !errors.Is(err, ErrCacheFull) {
				b.Fatalf("unique rejection: %v %v", fresh, err)
			}
		}
	})
}

// BenchmarkStorePruneExpired isolates the expiry-cohort cost that a combined
// refill/prune measurement can hide. Use -benchtime=100x: excluded refill work
// must not be scaled without bound by calibration of a much shorter prune call.
// This file is identical on the scan baseline and indexed candidate.
func BenchmarkStorePruneExpired(b *testing.B) {
	b.StopTimer()
	cfg := DefaultConfig()
	if cfg.CacheCount != 1024 || cfg.CacheBytes != 16<<20 || cfg.CacheTTL != 120*time.Second {
		b.Fatal("benchmark requires documented default cache limits")
	}
	txs, _ := loadFixtures(b, cfg.CacheCount)
	for _, tx := range txs {
		_ = tx.Hash()
		_ = tx.Size()
	}
	base := time.Unix(1700000000, 0)
	cutoff := base.Add(cfg.CacheTTL + 1023*time.Millisecond)
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		s := newStore(cfg.CacheCount, cfg.CacheBytes, cfg.CacheTTL)
		for j, tx := range txs {
			// 701 is coprime to 1024: deadlines are distinct and staggered,
			// with deliberately non-monotonic insertion times. The full span
			// is below TTL, so no fixture expires during setup.
			now := base.Add(time.Duration((j*701)%1024) * time.Millisecond)
			if fresh, err := s.add(tx, now); !fresh || err != nil {
				b.Fatalf("prune fixture setup: %v %v", fresh, err)
			}
		}
		b.StartTimer()
		s.prune(cutoff) // One operation reclaims all 1024 entries, including equality.
		b.StopTimer()
		// Check without stats/get, which would themselves prune and could mask
		// incomplete reclamation. These pre-index fields exist in both sources.
		s.mu.Lock()
		count, size := len(s.entries), s.bytes
		s.mu.Unlock()
		if count != 0 || size != 0 {
			b.Fatalf("prune retained count=%d bytes=%d", count, size)
		}
	}
	b.ReportMetric(float64(cfg.CacheCount), "entries/op")
}
