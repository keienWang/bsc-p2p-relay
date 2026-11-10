// SPDX-License-Identifier: GPL-3.0-or-later

// Package firstepoch provides an offline, first-epoch Parlia consensus-header
// experiment. It is not a live light client, execution verifier or finality API.
package firstepoch

import (
	"encoding/json"
	"errors"
	"fmt"
	"math/big"
	"sync"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/consensus/parlia"
	"github.com/ethereum/go-ethereum/core/rawdb"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/params"
	"github.com/ethereum/go-ethereum/rlp"
)

const (
	// MaxHeaders includes genesis. Advancement stops before the first epoch change.
	MaxHeaders = 200
	// MaxHeaderBytes bounds encoded input before RLP decoding, including genesis.
	MaxHeaderBytes = 2048
)

var (
	ErrLimit       = errors.New("header retention limit reached")
	ErrUnsupported = errors.New("unsupported header era or network")
	ErrAnchor      = errors.New("header does not match the pinned genesis")
	ErrAncestry    = errors.New("header is not the direct successor of the authenticated tip")
)

// Verifier retains one linear chain, with no reorg/fork choice or persistence.
// Methods serialize work; callers must not mutate input bytes concurrently.
// The zero value is unusable. Create a new instance with New.
type Verifier struct {
	mu    sync.Mutex
	chain *reader
	limit int
}

// New authenticates genesis RLP against the pinned official chain 56 or 97 hash.
// No caller-provided configuration, checkpoint or snapshot can establish trust.
// maxHeaders must be 2..MaxHeaders and includes genesis. Neither chain can
// advance beyond block 199, regardless of the supplied budget.
func New(chainID uint64, genesisRLP []byte, maxHeaders int) (*Verifier, error) {
	if maxHeaders < 2 || maxHeaders > MaxHeaders {
		return nil, ErrLimit
	}
	var cfg *params.ChainConfig
	var anchor common.Hash
	switch chainID {
	case 56:
		cfg, anchor = params.BSCChainConfig, params.BSCGenesisHash
	case 97:
		cfg, anchor = params.ChapelChainConfig, params.ChapelGenesisHash
	default:
		return nil, ErrUnsupported
	}
	h, err := decode(genesisRLP)
	if err != nil {
		return nil, err
	}
	if h.Number.Sign() != 0 || h.Hash() != anchor {
		return nil, ErrAnchor
	}
	return newAnchored(cfg, h, maxHeaders)
}

// newAnchored is private so synthetic anchors are available only to package tests.
func newAnchored(cfg *params.ChainConfig, genesis *types.Header, limit int) (*Verifier, error) {
	if limit < 2 || limit > MaxHeaders {
		return nil, ErrLimit
	}
	// Copy all pointer-valued fork fields; later mutations to upstream globals must
	// not alter an existing verifier's interpretation of its authenticated history.
	b, err := json.Marshal(cfg)
	if err != nil {
		return nil, err
	}
	var pinned params.ChainConfig
	if err := json.Unmarshal(b, &pinned); err != nil {
		return nil, err
	}
	return &Verifier{chain: &reader{cfg: &pinned, headers: []*types.Header{types.CopyHeader(genesis)}}, limit: limit}, nil
}

func decode(encoded []byte) (*types.Header, error) {
	if len(encoded) == 0 || len(encoded) > MaxHeaderBytes {
		return nil, ErrLimit
	}
	var h types.Header
	if err := rlp.DecodeBytes(encoded, &h); err != nil {
		return nil, fmt.Errorf("header RLP: %w", err)
	}
	if h.Number == nil || !h.Number.IsUint64() || h.Difficulty == nil || h.Difficulty.BitLen() > 256 {
		return nil, ErrUnsupported
	}
	return &h, nil
}

// Append checks a direct successor using the pinned official Parlia engine and
// retains it only on success. The returned header is an independent copy.
// Success authenticates the proposer/header under the genesis validator set,
// not execution, inclusion, canonicality, confirmations or BLS finality.
func (v *Verifier) Append(encoded []byte) (*types.Header, error) {
	v.mu.Lock()
	defer v.mu.Unlock()
	if v.chain == nil {
		return nil, ErrAnchor
	}
	if len(v.chain.headers) >= v.limit {
		return nil, ErrLimit
	}
	h, err := decode(encoded)
	if err != nil {
		return nil, err
	}
	n := h.Number.Uint64()
	// Freeze the scope before any epoch transition, time fork, or optional fields.
	// In the pinned mainnet/Chapel configs Shanghai is the first time fork, and
	// there are no block-based fork transitions in blocks 1..199.
	if n == 0 || n >= MaxHeaders || len(h.Extra) != 32+65 ||
		(v.chain.cfg.ShanghaiTime != nil && h.Time >= *v.chain.cfg.ShanghaiTime) ||
		h.BaseFee != nil || h.WithdrawalsHash != nil || h.BlobGasUsed != nil ||
		h.ExcessBlobGas != nil || h.ParentBeaconRoot != nil || h.RequestsHash != nil ||
		h.BlockAccessListHash != nil || h.SlotNumber != nil {
		return nil, ErrUnsupported
	}
	parent := v.chain.CurrentHeader()
	if n != parent.Number.Uint64()+1 || h.ParentHash != parent.Hash() {
		return nil, ErrAncestry
	}
	// No persistent or caller-supplied snapshot DB. In this range upstream can
	// only write the genesis snapshot, and ancestry traversal is <=198 headers,
	// far below its long-ancestry trusted-checkpoint fallback. Dispose all engine
	// caches and DB state even on failure, preventing adversarial cache growth.
	db := rawdb.NewMemoryDatabase()
	defer db.Close()
	engine := parlia.New(v.chain.cfg, db, nil, v.chain.GenesisHeader().Hash())
	defer engine.Close()
	if err := engine.VerifyHeader(v.chain, h); err != nil {
		return nil, fmt.Errorf("Parlia header: %w", err)
	}
	v.chain.headers = append(v.chain.headers, h)
	return types.CopyHeader(h), nil
}

// Tip returns a copy of the last accepted header (genesis initially).
func (v *Verifier) Tip() *types.Header {
	v.mu.Lock()
	defer v.mu.Unlock()
	if v.chain == nil {
		return nil
	}
	return types.CopyHeader(v.chain.CurrentHeader())
}

// reader is deliberately private: only genesis and successful Append results
// enter it. Upstream never receives candidates as an ancestry source.
type reader struct {
	cfg     *params.ChainConfig
	headers []*types.Header
}

func (r *reader) Config() *params.ChainConfig  { return r.cfg }
func (r *reader) CurrentHeader() *types.Header { return r.headers[len(r.headers)-1] }
func (r *reader) GenesisHeader() *types.Header { return r.headers[0] }
func (r *reader) GetHeaderByNumber(n uint64) *types.Header {
	if n >= uint64(len(r.headers)) {
		return nil
	}
	return r.headers[n]
}
func (r *reader) GetHeader(hash common.Hash, n uint64) *types.Header {
	h := r.GetHeaderByNumber(n)
	if h != nil && h.Hash() == hash {
		return h
	}
	return nil
}
func (r *reader) GetHeaderByHash(hash common.Hash) *types.Header {
	for _, h := range r.headers {
		if h.Hash() == hash {
			return h
		}
	}
	return nil
}
func (r *reader) GetTd(common.Hash, uint64) *big.Int      { return nil }
func (r *reader) GetHighestVerifiedHeader() *types.Header { return r.CurrentHeader() }
func (r *reader) GetVerifiedBlockByHash(hash common.Hash) *types.Header {
	return r.GetHeaderByHash(hash)
}
