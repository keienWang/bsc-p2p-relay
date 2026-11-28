// SPDX-License-Identifier: GPL-3.0-or-later
package relay

import (
	"bytes"
	"container/heap"
	"errors"
	"fmt"
	"math/big"
	"sync"
	"time"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/types"
)

const MaxRawTransaction = 128 * 1024

var ErrCacheFull = errors.New("transaction memory cache is full")

type cacheEntry struct {
	hash    common.Hash
	tx      *types.Transaction
	expires time.Time
	size    int
	index   int // Exactly one expiry-heap slot while the entry is cached.
}

// cacheExpiryHeap orders deadlines, not insertion times. Callers may supply
// non-increasing times to add, and multiple entries may have the same deadline.
// All heap and entry mutations are protected by store.mu.
type cacheExpiryHeap []*cacheEntry

func (h cacheExpiryHeap) Len() int           { return len(h) }
func (h cacheExpiryHeap) Less(i, j int) bool { return h[i].expires.Before(h[j].expires) }
func (h cacheExpiryHeap) Swap(i, j int) {
	h[i], h[j] = h[j], h[i]
	h[i].index, h[j].index = i, j
}
func (h *cacheExpiryHeap) Push(value any) {
	e := value.(*cacheEntry)
	e.index = len(*h)
	*h = append(*h, e)
}
func (h *cacheExpiryHeap) Pop() any {
	old := *h
	e := old[len(old)-1]
	old[len(old)-1] = nil // Do not retain a removed transaction in the backing array.
	e.index = -1
	*h = old[:len(old)-1]
	return e
}

type store struct {
	mu                        sync.Mutex
	entries                   map[common.Hash]*cacheEntry
	expiries                  cacheExpiryHeap
	expiryUpperBound          time.Time // >= every cached deadline; may be stale after removal.
	bytes, maxBytes, maxCount int
	ttl                       time.Duration
}

func newStore(count, bytes int, ttl time.Duration) *store {
	return &store{entries: make(map[common.Hash]*cacheEntry), maxCount: count, maxBytes: bytes, ttl: ttl}
}
func (s *store) pruneLocked(now time.Time) {
	if len(s.expiries) == 0 || now.Before(s.expiries[0].expires) {
		return
	}
	// The bound only rises while nonempty. Removing its original entry may
	// leave it too late, never too early: now >= bound proves every entry is due.
	// Avoid N heap repairs when the entire cohort can be released together.
	if !now.Before(s.expiryUpperBound) {
		for _, e := range s.expiries {
			e.index = -1
		}
		clear(s.entries)
		clear(s.expiries)
		s.expiries = s.expiries[:0]
		s.bytes = 0
		s.expiryUpperBound = time.Time{}
		return
	}
	for len(s.expiries) > 0 && !now.Before(s.expiries[0].expires) {
		s.removeLocked(s.expiries[0])
	}
}

func (s *store) removeLocked(e *cacheEntry) {
	heap.Remove(&s.expiries, e.index)
	delete(s.entries, e.hash)
	s.bytes -= e.size
	if len(s.entries) == 0 {
		s.expiryUpperBound = time.Time{}
	}
}

// release drops the in-memory cache backing once admission has stopped.
// Locked reads and pruning still observe an empty store.
func (s *store) release() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.entries = nil
	s.expiries = nil
	s.bytes = 0
	s.expiryUpperBound = time.Time{}
}

func (s *store) prune(now time.Time) { s.mu.Lock(); defer s.mu.Unlock(); s.pruneLocked(now) }

// addWithExpiry returns the lifetime selected by admission under the same
// lock as the cache lookup. Duplicate submissions do not extend that lifetime.
func (s *store) addWithExpiry(tx *types.Transaction, now time.Time) (bool, time.Time, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.pruneLocked(now)
	hash := tx.Hash()
	if e := s.entries[hash]; e != nil {
		return false, e.expires, nil
	}
	size := int(tx.Size())
	if len(s.entries) >= s.maxCount || size > s.maxBytes-s.bytes {
		return false, time.Time{}, ErrCacheFull
	}
	// Grow only after admission succeeds, and cap even the backing array at
	// maxCount. Rollbacks remove their slot immediately; there are no tombstones.
	if len(s.expiries) == cap(s.expiries) {
		capacity := cap(s.expiries)
		capacity += min(max(1, capacity), s.maxCount-capacity)
		expiries := make(cacheExpiryHeap, len(s.expiries), capacity)
		copy(expiries, s.expiries)
		s.expiries = expiries
	}
	e := &cacheEntry{hash: hash, tx: tx, expires: now.Add(s.ttl), size: size}
	if len(s.entries) == 0 || s.expiryUpperBound.Before(e.expires) {
		s.expiryUpperBound = e.expires
	}
	s.entries[hash] = e
	heap.Push(&s.expiries, e)
	s.bytes += size
	return true, e.expires, nil
}
func (s *store) get(h common.Hash, now time.Time) *types.Transaction {
	s.mu.Lock()
	defer s.mu.Unlock()
	if entry := s.getEntryLocked(h, now); entry != nil {
		return entry.tx
	}
	return nil
}

func (s *store) getEntryLocked(h common.Hash, now time.Time) *cacheEntry {
	e := s.entries[h]
	if e != nil && !now.Before(e.expires) {
		s.removeLocked(e)
		return nil
	}
	return e
}

// pooledTransactions takes one cache lock for the bounded list already
// validated by the wire handler. Transaction size was recorded at admission;
// encoding and transport run only after unlocking.
func (s *store) pooledTransactions(hashes []common.Hash, now time.Time, maxBytes int) types.Transactions {
	if len(hashes) == 0 {
		return nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	var txs types.Transactions
	for _, hash := range hashes {
		e := s.getEntryLocked(hash, now)
		if e == nil {
			continue
		}
		if e.size > maxBytes {
			break
		}
		txs = append(txs, e.tx)
		maxBytes -= e.size
	}
	return txs
}
func (s *store) stats() (int, int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	// Empty caches have no deadlines to compare. Skip the clock read
	// when no transaction has been cached or the last entry has expired.
	if len(s.expiries) != 0 {
		s.pruneLocked(time.Now())
	}
	return len(s.entries), s.bytes
}

// validate performs stateless checks only. It cannot establish affordability,
// next nonce, fork activation, intrinsic gas, mempool acceptance or execution.
func validate(raw []byte, chainID *big.Int) (*types.Transaction, common.Address, error) {
	var zero common.Address
	if len(raw) == 0 || len(raw) > MaxRawTransaction {
		return nil, zero, errors.New("raw transaction size must be 1..131072 bytes")
	}
	var tx types.Transaction
	if err := tx.UnmarshalBinary(raw); err != nil {
		return nil, zero, fmt.Errorf("decode signed transaction: %w", err)
	}
	switch tx.Type() {
	case types.LegacyTxType, types.AccessListTxType, types.DynamicFeeTxType:
	default:
		return nil, zero, errors.New("only legacy, access-list and dynamic-fee transactions are supported in this milestone")
	}
	if !tx.Protected() || tx.ChainId().Cmp(chainID) != 0 {
		return nil, zero, errors.New("transaction must be replay-protected and match the configured chain ID")
	}
	if tx.Gas() < 21000 {
		return nil, zero, errors.New("gas limit is below 21000")
	}
	// The transaction getters return defensive copies. Read each cap once
	// for both the width and ordering checks.
	fee, tip := tx.GasFeeCap(), tx.GasTipCap()
	if tx.Value().BitLen() > 256 || fee.BitLen() > 256 || tip.BitLen() > 256 {
		return nil, zero, errors.New("transaction value or fee exceeds uint256")
	}
	if tip.Cmp(fee) > 0 {
		return nil, zero, errors.New("max priority fee exceeds max fee")
	}
	canonical, err := tx.MarshalBinary()
	if err != nil || !bytes.Equal(canonical, raw) {
		return nil, zero, errors.New("non-canonical transaction encoding")
	}
	sender, err := types.Sender(types.LatestSignerForChainID(chainID), &tx)
	if err != nil {
		return nil, zero, fmt.Errorf("invalid signature: %w", err)
	}
	return &tx, sender, nil
}

func (s *store) remove(h common.Hash) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if e, ok := s.entries[h]; ok {
		s.removeLocked(e)
	}
}

func (s *store) expiry(h common.Hash) time.Time {
	s.mu.Lock()
	defer s.mu.Unlock()
	if e, ok := s.entries[h]; ok {
		return e.expires
	}
	return time.Time{}
}
