// SPDX-License-Identifier: GPL-3.0-or-later
package relay

import (
	"bytes"
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
	tx      *types.Transaction
	expires time.Time
	size    int
}
type store struct {
	mu                        sync.Mutex
	entries                   map[common.Hash]cacheEntry
	bytes, maxBytes, maxCount int
	ttl                       time.Duration
}

func newStore(count, bytes int, ttl time.Duration) *store {
	return &store{entries: make(map[common.Hash]cacheEntry), maxCount: count, maxBytes: bytes, ttl: ttl}
}
func (s *store) pruneLocked(now time.Time) {
	for h, e := range s.entries {
		if !now.Before(e.expires) {
			delete(s.entries, h)
			s.bytes -= e.size
		}
	}
}
func (s *store) prune(now time.Time) { s.mu.Lock(); defer s.mu.Unlock(); s.pruneLocked(now) }
func (s *store) add(tx *types.Transaction, now time.Time) (bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.pruneLocked(now)
	if _, ok := s.entries[tx.Hash()]; ok {
		return false, nil
	}
	size := int(tx.Size())
	if len(s.entries) >= s.maxCount || size > s.maxBytes-s.bytes {
		return false, ErrCacheFull
	}
	s.entries[tx.Hash()] = cacheEntry{tx, now.Add(s.ttl), size}
	s.bytes += size
	return true, nil
}
func (s *store) get(h common.Hash, now time.Time) *types.Transaction {
	s.mu.Lock()
	defer s.mu.Unlock()
	e, ok := s.entries[h]
	if !ok {
		return nil
	}
	if !now.Before(e.expires) {
		delete(s.entries, h)
		s.bytes -= e.size
		return nil
	}
	return e.tx
}
func (s *store) stats() (int, int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.pruneLocked(time.Now())
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
	if tx.Value().BitLen() > 256 || tx.GasFeeCap().BitLen() > 256 || tx.GasTipCap().BitLen() > 256 {
		return nil, zero, errors.New("transaction value or fee exceeds uint256")
	}
	if tx.GasTipCap().Cmp(tx.GasFeeCap()) > 0 {
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
		delete(s.entries, h)
		s.bytes -= e.size
	}
}

func (s *store) expiry(h common.Hash) time.Time {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.entries[h].expires
}
