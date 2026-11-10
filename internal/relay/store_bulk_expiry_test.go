// SPDX-License-Identifier: GPL-3.0-or-later
package relay

import (
	"testing"
	"time"
)

func TestStoreBulkExpiryKeepsFutureEntry(t *testing.T) {
	txs, _ := loadFixtures(t, 3)
	s := newStore(3, 16<<20, 10*time.Second)
	base := time.Unix(1700000000, 0)
	for i, offset := range []time.Duration{0, time.Second, 5 * time.Second} {
		if _, err := s.add(txs[i], base.Add(offset)); err != nil {
			t.Fatal(err)
		}
	}
	first, second := s.entries[txs[0].Hash()], s.entries[txs[1].Hash()]
	cutoff := base.Add(11 * time.Second)
	if !cutoff.Before(s.expiryUpperBound) {
		t.Fatal("future entry did not prevent bulk-clear eligibility")
	}
	s.prune(cutoff)
	if len(s.entries) != 1 || s.bytes != int(txs[2].Size()) || s.get(txs[2].Hash(), cutoff) == nil {
		t.Fatal("prune removed a future live entry or kept a due entry")
	}
	if first.index != -1 || second.index != -1 {
		t.Fatal("individual expiry retained removed indices")
	}
	assertStoreIndex(t, s)
}

func TestStoreBulkExpiryStaleUpperBound(t *testing.T) {
	txs, _ := loadFixtures(t, 3)
	s := newStore(3, 16<<20, 10*time.Second)
	base := time.Unix(1700000000, 0)
	for i, offset := range []time.Duration{0, 3 * time.Second, 5 * time.Second} {
		if _, err := s.add(txs[i], base.Add(offset)); err != nil {
			t.Fatal(err)
		}
	}
	upper := s.expiryUpperBound
	s.remove(txs[2].Hash()) // Removing the maximum does not scan to tighten it.
	if !s.expiryUpperBound.Equal(upper) || !upper.Equal(base.Add(15*time.Second)) {
		t.Fatal("removal did not retain the conservative upper bound")
	}
	s.prune(base.Add(12 * time.Second))
	if len(s.entries) != 1 || s.get(txs[1].Hash(), base.Add(12*time.Second)) == nil {
		t.Fatal("stale upper bound caused premature clearance")
	}
	assertStoreIndex(t, s)
	// All actual remaining entries are now due, but the stale bound deliberately
	// rules out the fast path. Ordinary heap removal must still finish cleanup.
	actualLastDeadline := base.Add(13 * time.Second)
	if !actualLastDeadline.Before(s.expiryUpperBound) {
		t.Fatal("fixture did not retain an overly late bound")
	}
	last := s.entries[txs[1].Hash()]
	s.prune(actualLastDeadline)
	if len(s.entries) != 0 || s.bytes != 0 || !s.expiryUpperBound.IsZero() || last.index != -1 {
		t.Fatal("fallback did not fully clear and reset the empty store")
	}
	assertStoreIndex(t, s)
}

func TestStoreBulkExpiryClearsReferencesAndReinserts(t *testing.T) {
	txs, _ := loadFixtures(t, 7)
	for _, base := range []time.Time{time.Unix(1700000000, 0), time.Time{}.Add(-20 * time.Second)} {
		s := newStore(7, 16<<20, 10*time.Second)
		var removed []*cacheEntry
		for i, offset := range []time.Duration{4, 0, 6, 1, 5, 3, 2} {
			if fresh, err := s.add(txs[i], base.Add(offset*time.Second)); !fresh || err != nil {
				t.Fatalf("non-monotonic fixture: %v %v", fresh, err)
			}
			removed = append(removed, s.entries[txs[i].Hash()])
			assertStoreIndex(t, s)
		}
		cutoff := base.Add(16 * time.Second)
		if !s.expiryUpperBound.Equal(cutoff) {
			t.Fatal("insertion did not maintain the maximum deadline")
		}
		backing := s.expiries[:cap(s.expiries)]
		s.prune(cutoff) // Equality at the maximum proves the entire cohort is due.
		if len(s.entries) != 0 || s.bytes != 0 || len(s.expiries) != 0 || !s.expiryUpperBound.IsZero() {
			t.Fatal("bulk expiry did not clear every store representation")
		}
		for _, e := range removed {
			if e.index != -1 {
				t.Fatal("bulk expiry left a removed entry indexed")
			}
		}
		for _, e := range backing {
			if e != nil {
				t.Fatal("bulk expiry backing array still retains a transaction entry")
			}
		}
		assertStoreIndex(t, s)
		// Even a backwards-time reinsert must start a new independent bound,
		// rather than retaining the previous cohort's later deadline.
		if fresh, err := s.add(txs[0], base); !fresh || err != nil {
			t.Fatalf("reinsert after bulk clear: %v %v", fresh, err)
		}
		if !s.expiryUpperBound.Equal(base.Add(s.ttl)) || s.entries[txs[0].Hash()] == removed[0] {
			t.Fatal("reinsert reused stale entry identity or deadline bound")
		}
		assertStoreIndex(t, s)
	}
}

func TestStoreBulkExpiryGetAndRemoveStayTargeted(t *testing.T) {
	txs, _ := loadFixtures(t, 2)
	s := newStore(2, 16<<20, time.Second)
	base := time.Unix(1700000000, 0)
	for _, tx := range txs {
		if _, err := s.add(tx, base); err != nil {
			t.Fatal(err)
		}
	}
	deadline := base.Add(time.Second)
	if s.get(txs[0].Hash(), deadline) != nil || len(s.entries) != 1 || s.expiry(txs[1].Hash()).IsZero() {
		t.Fatal("get bulk-cleared another expired entry instead of only its target")
	}
	assertStoreIndex(t, s)
	s.remove(txs[1].Hash())
	if !s.expiryUpperBound.IsZero() {
		t.Fatal("last rollback/removal did not reset the upper bound")
	}
	assertStoreIndex(t, s)
	if fresh, err := s.add(txs[1], base.Add(-time.Hour)); !fresh || err != nil {
		t.Fatal("same hash could not start a new lifetime after remove")
	}
	if !s.expiryUpperBound.Equal(base.Add(-time.Hour + s.ttl)) {
		t.Fatal("same-hash reinsert inherited the previous deadline bound")
	}
	assertStoreIndex(t, s)
}

func TestStoreBulkExpiryTestHelperMaintainsBound(t *testing.T) {
	txs, _ := loadFixtures(t, 2)
	s := newStore(2, 16<<20, time.Second)
	base := time.Unix(1700000000, 0)
	for _, tx := range txs {
		if _, err := s.add(tx, base); err != nil {
			t.Fatal(err)
		}
	}
	later := base.Add(time.Hour)
	setCacheExpiryForTest(t, s, later, txs[0].Hash())
	if !s.expiryUpperBound.Equal(later) {
		t.Fatal("test helper extension invalidated the expiry upper bound")
	}
	s.prune(base.Add(2 * time.Second))
	if len(s.entries) != 1 || s.get(txs[0].Hash(), base.Add(2*time.Second)) == nil {
		t.Fatal("test helper allowed bulk-clear of a rewritten live deadline")
	}
	setCacheExpiryForTest(t, s, base) // Existing negative fixtures move all deadlines earlier.
	if !s.expiryUpperBound.Equal(base) {
		t.Fatal("all-entry test helper did not rebuild the bound")
	}
	s.prune(base)
	assertStoreIndex(t, s)
}
