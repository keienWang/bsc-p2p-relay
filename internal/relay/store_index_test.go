// SPDX-License-Identifier: GPL-3.0-or-later
package relay

import (
	"container/heap"
	"errors"
	"fmt"
	"math/rand"
	"sync"
	"testing"
	"time"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/types"
)

// setCacheExpiryForTest replaces test-only clock mutation at five existing
// regression sites. The production API cannot rewrite an admitted deadline.
// With no hashes, it changes all entries. Callers retain their original peer
// sent-map changes and assertions; this helper also keeps the expiry index and
// conservative upper bound valid after rewriting a production-immutable field.
func setCacheExpiryForTest(t testing.TB, s *store, expires time.Time, hashes ...common.Hash) {
	t.Helper()
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(hashes) == 0 {
		for _, e := range s.entries {
			e.expires = expires
		}
		heap.Init(&s.expiries)
	} else {
		for _, hash := range hashes {
			e, ok := s.entries[hash]
			if !ok {
				t.Fatalf("expiry fixture has no entry %s", hash)
			}
			e.expires = expires
			heap.Fix(&s.expiries, e.index)
		}
	}
	s.expiryUpperBound = time.Time{}
	first := true
	for _, e := range s.entries {
		if first || s.expiryUpperBound.Before(e.expires) {
			s.expiryUpperBound = e.expires
			first = false
		}
	}
	if err := cacheInvariantLocked(s); err != nil {
		t.Fatal(err)
	}
}

// cacheInvariantLocked checks both directions of the map/index relationship,
// accounting, heap ordering, hard backing-array capacity and cleared spare slots.
// It is shared by store, load and unique-capacity tests. Caller holds s.mu.
func cacheInvariantLocked(s *store) error {
	if len(s.entries) != len(s.expiries) || len(s.entries) > s.maxCount || cap(s.expiries) > s.maxCount {
		return fmt.Errorf("cache index bound: map=%d heap=%d capacity=%d limit=%d", len(s.entries), len(s.expiries), cap(s.expiries), s.maxCount)
	}
	if len(s.entries) == 0 && !s.expiryUpperBound.IsZero() {
		return fmt.Errorf("empty cache retained expiry upper bound %s", s.expiryUpperBound)
	}
	total := 0
	for hash, e := range s.entries {
		if e == nil || e.hash != hash || e.tx == nil || e.tx.Hash() != hash || e.size != int(e.tx.Size()) {
			return fmt.Errorf("cache entry identity/size mismatch for %s", hash)
		}
		if e.index < 0 || e.index >= len(s.expiries) || s.expiries[e.index] != e {
			return fmt.Errorf("cache entry has invalid heap index for %s", hash)
		}
		if s.expiryUpperBound.Before(e.expires) {
			return fmt.Errorf("cache expiry upper bound precedes entry %s", hash)
		}
		total += e.size
	}
	if total != s.bytes || total < 0 || total > s.maxBytes {
		return fmt.Errorf("cache byte accounting: entries=%d recorded=%d limit=%d", total, s.bytes, s.maxBytes)
	}
	for i, e := range s.expiries {
		if e == nil || e.index != i || s.entries[e.hash] != e {
			return fmt.Errorf("cache heap slot %d is missing, stale or duplicated", i)
		}
		if i > 0 && e.expires.Before(s.expiries[(i-1)/2].expires) {
			return fmt.Errorf("cache heap deadline order at slot %d", i)
		}
	}
	for i, e := range s.expiries[len(s.expiries):cap(s.expiries)] {
		if e != nil {
			return fmt.Errorf("cache heap retains removed entry in spare slot %d", len(s.expiries)+i)
		}
	}
	return nil
}

func assertStoreIndex(t testing.TB, s *store) {
	t.Helper()
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := cacheInvariantLocked(s); err != nil {
		t.Fatal(err)
	}
}

// scanStoreReference intentionally retains the pre-index map-scan algorithm.
// It has independent entries and accounting and never uses production heap code.
type scanStoreReference struct {
	entries                   map[common.Hash]scanCacheEntry
	bytes, maxBytes, maxCount int
	ttl                       time.Duration
}

type scanCacheEntry struct {
	tx      *types.Transaction
	expires time.Time
	size    int
}

func newScanStoreReference(count, bytes int, ttl time.Duration) *scanStoreReference {
	return &scanStoreReference{entries: make(map[common.Hash]scanCacheEntry), maxCount: count, maxBytes: bytes, ttl: ttl}
}

func (s *scanStoreReference) prune(now time.Time) {
	for hash, e := range s.entries {
		if !now.Before(e.expires) {
			delete(s.entries, hash)
			s.bytes -= e.size
		}
	}
}

func (s *scanStoreReference) add(tx *types.Transaction, now time.Time) (bool, error) {
	s.prune(now)
	if _, ok := s.entries[tx.Hash()]; ok {
		return false, nil
	}
	size := int(tx.Size())
	if len(s.entries) >= s.maxCount || size > s.maxBytes-s.bytes {
		return false, ErrCacheFull
	}
	s.entries[tx.Hash()] = scanCacheEntry{tx, now.Add(s.ttl), size}
	s.bytes += size
	return true, nil
}

func (s *scanStoreReference) get(hash common.Hash, now time.Time) *types.Transaction {
	e, ok := s.entries[hash]
	if !ok {
		return nil
	}
	if !now.Before(e.expires) {
		s.remove(hash)
		return nil
	}
	return e.tx
}

func (s *scanStoreReference) remove(hash common.Hash) {
	if e, ok := s.entries[hash]; ok {
		delete(s.entries, hash)
		s.bytes -= e.size
	}
}

func compareStoreReference(t *testing.T, s *store, reference *scanStoreReference, step int) {
	t.Helper()
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := cacheInvariantLocked(s); err != nil {
		t.Fatalf("step %d: %v", step, err)
	}
	if len(s.entries) != len(reference.entries) || s.bytes != reference.bytes {
		t.Fatalf("step %d: indexed count/bytes %d/%d, scan %d/%d", step, len(s.entries), s.bytes, len(reference.entries), reference.bytes)
	}
	for hash, want := range reference.entries {
		got, ok := s.entries[hash]
		if !ok || got.tx != want.tx || !got.expires.Equal(want.expires) || got.size != want.size {
			t.Fatalf("step %d: differing entry for %s", step, hash)
		}
	}
}

func TestStoreIndexScanReference(t *testing.T) {
	txs, _ := loadFixtures(t, 65)
	for _, tc := range []struct {
		name  string
		count int
		bytes int
		ttl   time.Duration
	}{
		{"count_one", 1, 16 << 20, time.Second},
		{"non_power_of_two", 7, 16 << 20, time.Minute},
		{"bytes_exact", 31, int(txs[0].Size() + txs[1].Size()), time.Second},
		{"bytes_one_short", 31, int(txs[0].Size()) - 1, time.Second},
		{"short_ttl", 31, 16 << 20, time.Nanosecond},
		{"zero_capacity", 0, 0, time.Second},
	} {
		for _, seed := range []int64{7, 20261110} {
			t.Run(fmt.Sprintf("%s/seed_%d", tc.name, seed), func(t *testing.T) {
				s := newStore(tc.count, tc.bytes, tc.ttl)
				reference := newScanStoreReference(tc.count, tc.bytes, tc.ttl)
				rng := rand.New(rand.NewSource(seed))
				base := time.Unix(1700000000, 0)
				for step := 0; step < 2000; step++ {
					tx := txs[rng.Intn(len(txs))]
					// Mostly dense live cohorts, interspersed with larger clock
					// jumps. Independent offsets move backwards and repeat.
					now := base.Add(time.Duration(rng.Intn(9)-4) * tc.ttl / 16)
					if step%41 == 0 {
						now = base.Add(time.Duration(rng.Intn(9)-4) * tc.ttl)
					}
					if step%5 == 0 {
						if e, ok := reference.entries[tx.Hash()]; ok {
							now = e.expires // Equality must expire, not retain.
						}
					}
					op := rng.Intn(10)
					if step%257 == 256 {
						op = 10
					}
					switch op {
					case 0, 1, 2, 3, 4, 5:
						got, err := s.add(tx, now)
						want, wantErr := reference.add(tx, now)
						if got != want || !errors.Is(err, wantErr) {
							t.Fatalf("step %d add: got %v/%v, want %v/%v", step, got, err, want, wantErr)
						}
					case 6:
						if got, want := s.get(tx.Hash(), now), reference.get(tx.Hash(), now); got != want {
							t.Fatalf("step %d get differs", step)
						}
					case 7:
						s.remove(tx.Hash())
						reference.remove(tx.Hash())
					case 8:
						s.prune(now)
						reference.prune(now)
					case 9:
						if got, want := s.expiry(tx.Hash()), reference.entries[tx.Hash()].expires; !got.Equal(want) {
							t.Fatalf("step %d expiry: got %s want %s", step, got, want)
						}
					case 10:
						// All generated deadlines are far in the past, so stats is
						// deterministic despite using the production wall clock.
						reference.prune(time.Now())
						count, bytes := s.stats()
						if count != len(reference.entries) || bytes != reference.bytes {
							t.Fatalf("step %d stats differs", step)
						}
					}
					compareStoreReference(t, s, reference, step)
				}
			})
		}
	}
}

func TestStoreIndexDeadlinesAndTargetGet(t *testing.T) {
	txs, _ := loadFixtures(t, 4)
	base := time.Unix(1700000000, 0)
	s := newStore(4, 16<<20, 10*time.Second)
	// Later insertion can expire earlier; two entries share the first deadline.
	for i, offset := range []time.Duration{4, 0, 0, 2} {
		if fresh, err := s.add(txs[i], base.Add(offset*time.Second)); !fresh || err != nil {
			t.Fatalf("add %d: %v %v", i, fresh, err)
		}
	}
	assertStoreIndex(t, s)
	deadline := base.Add(10 * time.Second)
	if got := s.get(txs[1].Hash(), deadline.Add(-time.Nanosecond)); got == nil {
		t.Fatal("target expired before deadline")
	}
	if got := s.get(txs[1].Hash(), deadline); got != nil {
		t.Fatal("target retained at deadline")
	}
	// get only removes its expired target, not another expired entry.
	if s.expiry(txs[2].Hash()).IsZero() {
		t.Fatal("target get also removed unrelated entry")
	}
	assertStoreIndex(t, s)
	s.prune(deadline)
	if len(s.entries) != 2 || s.get(txs[0].Hash(), deadline) == nil || s.get(txs[3].Hash(), deadline) == nil {
		t.Fatal("same-deadline prune removed live entries or kept expired entries")
	}
	if fresh, err := s.add(txs[1], deadline); !fresh || err != nil || !s.expiry(txs[1].Hash()).Equal(deadline.Add(s.ttl)) {
		t.Fatalf("expired hash did not receive a fresh lifetime: %v %v", fresh, err)
	}
	if fresh, err := s.add(txs[1], deadline.Add(time.Second)); fresh || err != nil || !s.expiry(txs[1].Hash()).Equal(deadline.Add(s.ttl)) {
		t.Fatal("duplicate changed original deadline")
	}
	s.remove(txs[3].Hash())
	s.remove(txs[3].Hash())
	if !s.expiry(txs[3].Hash()).IsZero() {
		t.Fatal("removed hash has a deadline")
	}
	assertStoreIndex(t, s)
}

func TestStoreIndexRejectionAndRollbackBound(t *testing.T) {
	txs, _ := loadFixtures(t, 18)
	now := time.Now()
	for _, count := range []int{1, 3, 17} {
		t.Run(fmt.Sprintf("count_%d", count), func(t *testing.T) {
			s := newStore(count, 16<<20, time.Minute)
			for _, tx := range txs[:count] {
				if _, err := s.add(tx, now); err != nil {
					t.Fatal(err)
				}
			}
			capacity, bytes := cap(s.expiries), s.bytes
			for i := 0; i < 2000; i++ {
				if fresh, err := s.add(txs[count], now); fresh || !errors.Is(err, ErrCacheFull) {
					t.Fatalf("full count accepted: %v %v", fresh, err)
				}
				if fresh, err := s.add(txs[0], now); fresh || err != nil {
					t.Fatal("full count duplicate rejected")
				}
				if cap(s.expiries) != capacity || s.bytes != bytes {
					t.Fatal("rejected/duplicate add grew index or bytes")
				}
				// A queue rollback removes the fresh entry, not a tombstone.
				removed := s.entries[txs[0].Hash()]
				s.remove(txs[0].Hash())
				if removed.index != -1 {
					t.Fatal("rollback retained a heap index")
				}
				if fresh, err := s.add(txs[0], now); !fresh || err != nil {
					t.Fatal("remove/reinsert failed")
				}
				assertStoreIndex(t, s)
			}
			for _, tx := range txs[:count] {
				e := s.entries[tx.Hash()]
				s.remove(tx.Hash())
				if e.index != -1 {
					t.Fatal("removed entry retained a heap index")
				}
			}
			assertStoreIndex(t, s)
		})
	}
	// Byte admission is exact and cannot reserve an index on rejection.
	limit := int(txs[0].Size() + txs[1].Size())
	s := newStore(17, limit, time.Minute)
	for _, tx := range txs[:2] {
		if _, err := s.add(tx, now); err != nil {
			t.Fatal(err)
		}
	}
	capacity := cap(s.expiries)
	for i := 0; i < 2000; i++ {
		if fresh, err := s.add(txs[2], now); fresh || !errors.Is(err, ErrCacheFull) {
			t.Fatal("byte limit accepted new entry")
		}
		if cap(s.expiries) != capacity || s.bytes != limit {
			t.Fatal("byte rejection grew index or changed accounting")
		}
	}
	assertStoreIndex(t, s)
}

func TestStoreIndexStatsRetainsLive(t *testing.T) {
	txs, _ := loadFixtures(t, 2)
	s := newStore(2, 16<<20, time.Minute)
	now := time.Now()
	if _, err := s.add(txs[0], now); err != nil {
		t.Fatal(err)
	}
	// Insertion with an earlier clock leaves both entries until the next prune.
	if _, err := s.add(txs[1], now.Add(-time.Hour)); err != nil {
		t.Fatal(err)
	}
	if count, bytes := s.stats(); count != 1 || bytes != int(txs[0].Size()) {
		t.Fatalf("stats count/bytes=%d/%d", count, bytes)
	}
	if s.get(txs[0].Hash(), now) == nil || !s.expiry(txs[1].Hash()).IsZero() {
		t.Fatal("stats removed live entry or retained expired entry")
	}
	assertStoreIndex(t, s)
}

func TestStoreIndexConcurrentOperations(t *testing.T) {
	txs, _ := loadFixtures(t, 65)
	s := newStore(31, 16<<20, time.Nanosecond)
	base := time.Unix(1700000000, 0)
	start := make(chan struct{})
	var wg sync.WaitGroup
	for worker := 0; worker < 8; worker++ {
		wg.Add(1)
		go func(worker int) {
			defer wg.Done()
			<-start
			for i := 0; i < 500; i++ {
				tx := txs[(i+worker)%len(txs)]
				now := base.Add(time.Duration((i+worker)%7-3) * time.Nanosecond)
				switch (i + worker) % 6 {
				case 0:
					if _, err := s.add(tx, now); err != nil && !errors.Is(err, ErrCacheFull) {
						t.Error(err)
					}
				case 1:
					s.get(tx.Hash(), now)
				case 2:
					s.remove(tx.Hash())
				case 3:
					s.prune(now)
				case 4:
					s.expiry(tx.Hash())
				case 5:
					s.stats()
				}
				s.mu.Lock()
				err := cacheInvariantLocked(s)
				s.mu.Unlock()
				if err != nil {
					t.Error(err)
					return
				}
			}
		}(worker)
	}
	close(start)
	wg.Wait()
	s.prune(base.Add(time.Hour))
	assertStoreIndex(t, s)
	if len(s.entries) != 0 || s.bytes != 0 {
		t.Fatal("concurrent store did not fully reclaim")
	}
}
