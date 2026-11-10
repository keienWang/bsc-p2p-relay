//go:build canonicalisolation

// SPDX-License-Identifier: GPL-3.0-or-later
// A deliberately separate executable: BSC/Prysm package initialization occurs
// here, not in the stdlib-only supervising test binary. One request, then exit.
package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"math/big"
	"os"
	"runtime"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/consensus/parlia"
	"github.com/ethereum/go-ethereum/core/rawdb"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/params"
	"github.com/ethereum/go-ethereum/rlp"
	"github.com/keienWang/bsc-p2p-relay/internal/canonicalisolation/protocol"
)

func main() {
	if len(os.Args) == 2 && os.Args[1] == "--candidates" {
		candidateMain()
		return
	}
	raw, err := io.ReadAll(io.LimitReader(os.Stdin, protocol.MaxInput+1))
	result := protocol.Response{Stage: "admission", FixtureID: protocol.FixtureID, RequestDigest: protocol.Digest(raw)}
	if err != nil {
		result.Error = "cannot read request"
	} else if len(raw) > protocol.MaxInput {
		result.Error = "input exceeds 4 MiB"
	} else if runtime.GOMAXPROCS(0) != 2 {
		result.Error = "requires supervised GOMAXPROCS=2"
	} else {
		result = handle(raw, result)
	}
	if len(result.Error) > 512 {
		result.Error = result.Error[:512]
	}
	encoded, err := json.Marshal(result)
	if err != nil || len(encoded)+1 > protocol.MaxOutput {
		os.Exit(3)
	}
	if _, err := os.Stdout.Write(append(encoded, '\n')); err != nil {
		os.Exit(3)
	}
	if !result.Accepted {
		os.Exit(2)
	}
}

func handle(raw []byte, result protocol.Response) protocol.Response {
	var req protocol.Request
	if err := protocol.StrictJSON(raw, &req); err != nil {
		result.Error = err.Error()
		return result
	}
	result.CandidateDigest = protocol.Digest(req.Candidate)
	if req.FixtureID != protocol.FixtureID || len(req.Retained) > protocol.MaxRetained || len(req.Snapshots) > protocol.MaxSnapshots || len(req.Candidate) == 0 || len(req.Candidate) > protocol.MaxRecord {
		result.Error = "request outside fixture bounds"
		return result
	}
	// Authenticate ALL context bytes before parsing headers or snapshot JSON.
	cfg, headers, snaps, err := decodeBundle(req.Bundle)
	if err != nil {
		result.Error = err.Error()
		return result
	}
	h, err := decodeHeader(req.Candidate)
	if err != nil {
		result.Error = err.Error()
		return result
	}
	if h.Number.Uint64() != protocol.CandidateNumber {
		result.Error = "candidate outside synthetic height bound"
		return result
	}
	c := &chain{cfg: cfg, byNumber: map[uint64]*types.Header{}, byHash: map[common.Hash]*types.Header{}}
	c.add(headers[0])
	var last uint64
	for _, n := range req.Retained {
		if n < 2000 || n >= protocol.CandidateNumber || n <= last {
			result.Error = "retained headers must be unique ascending subset of [2000,3072]"
			return result
		}
		last = n
		if !req.Replay {
			c.add(headers[n])
		}
	}
	last = 0
	for _, n := range req.Snapshots {
		if (n != 2048 && n != 3072) || n <= last {
			result.Error = "invalid snapshot selection"
			return result
		}
		last = n
	}
	if req.Replay && (len(req.Retained) != protocol.MaxRetained || len(req.Snapshots) != protocol.MaxSnapshots) {
		result.Error = "full replay requires complete selections"
		return result
	}
	return verify(c, headers, snaps, h, req, result)
}

func decodeHeader(raw []byte) (*types.Header, error) {
	if len(raw) == 0 || len(raw) > protocol.MaxRecord {
		return nil, errors.New("header record exceeds bound")
	}
	var h types.Header
	if err := rlp.DecodeBytes(raw, &h); err != nil {
		return nil, err
	}
	if h.Number == nil || !h.Number.IsUint64() || h.Difficulty == nil || h.Difficulty.Sign() < 0 || h.Difficulty.BitLen() > 256 || (h.BaseFee != nil && (h.BaseFee.Sign() < 0 || h.BaseFee.BitLen() > 256)) {
		return nil, errors.New("invalid header integer")
	}
	// Byte equality is the canonical wire invariant; no JSON Header transport.
	again, err := rlp.EncodeToBytes(&h)
	if err != nil || !bytes.Equal(raw, again) {
		return nil, errors.New("noncanonical header RLP")
	}
	return &h, nil
}

func decodeBundle(bundle protocol.Bundle) (*params.ChainConfig, []*types.Header, map[uint64]*parlia.Snapshot, error) {
	if err := bundle.Authenticate(); err != nil {
		return nil, nil, nil, err
	}
	cfg := new(params.ChainConfig)
	if err := protocol.StrictJSON(bundle.Config, cfg); err != nil {
		return nil, nil, nil, err
	}
	if err := cfg.CheckConfigForkOrder(); err != nil {
		return nil, nil, nil, err
	}
	s := rlp.NewStream(bytes.NewReader(bundle.Headers), uint64(len(bundle.Headers)))
	if _, err := s.List(); err != nil {
		return nil, nil, nil, err
	}
	headers := make([]*types.Header, 0, protocol.HeaderCount)
	for i := 0; i < protocol.HeaderCount; i++ {
		raw, err := s.Raw()
		if err != nil {
			return nil, nil, nil, err
		}
		h, err := decodeHeader(raw)
		if err != nil {
			return nil, nil, nil, err
		}
		if h.Number.Uint64() != uint64(i) || (i > 0 && h.ParentHash != headers[i-1].Hash()) {
			return nil, nil, nil, errors.New("unlinked fixture headers")
		}
		headers = append(headers, h)
	}
	if err := s.ListEnd(); err != nil {
		return nil, nil, nil, err
	}
	if _, err := s.Raw(); err != io.EOF {
		return nil, nil, nil, errors.New("trailing fixture RLP")
	}
	again, err := rlp.EncodeToBytes(headers)
	if err != nil || !bytes.Equal(again, bundle.Headers) || headers[0].Hash().Hex() != protocol.GenesisHash {
		return nil, nil, nil, errors.New("fixture RLP round trip or genesis mismatch")
	}
	snaps := map[uint64]*parlia.Snapshot{}
	for n, raw := range map[uint64][]byte{2048: bundle.Support, 3072: bundle.Anchor} {
		snap, err := decodeSnapshot(raw, n, headers)
		if err != nil {
			return nil, nil, nil, err
		}
		snaps[n] = snap
	}
	// Same three validator identities/keys at both trusted snapshot boundaries.
	for addr, info := range snaps[2048].Validators {
		other := snaps[3072].Validators[addr]
		if other == nil || *info != *other {
			return nil, nil, nil, errors.New("validator set mismatch")
		}
	}
	return cfg, headers, snaps, nil
}

func decodeSnapshot(raw []byte, n uint64, headers []*types.Header) (*parlia.Snapshot, error) {
	if len(raw) > protocol.MaxRecord {
		return nil, errors.New("snapshot exceeds bound")
	}
	var s parlia.Snapshot
	if err := protocol.StrictJSON(raw, &s); err != nil {
		return nil, err
	}
	// Preserve upstream's literal tags "attestation:omitempty" and
	// "index:omitempty". Never silently normalize them or accept aliases.
	again, err := json.Marshal(&s)
	if err != nil || !bytes.Equal(raw, again) {
		return nil, errors.New("snapshot JSON is not exact canonical semantic form")
	}
	if s.Number != n || s.Hash != headers[n].Hash() || s.EpochLength != 1000 || s.BlockInterval != 450 || s.TurnLength != 32 || len(s.Validators) != protocol.MaxValidators || len(s.Recents) != 3 || len(s.RecentForkHashes) != 96 || s.Attestation == nil {
		return nil, errors.New("snapshot profile mismatch")
	}
	indexes := map[int]bool{}
	keys := map[types.BLSPublicKey]bool{}
	for addr, v := range s.Validators {
		if addr == (common.Address{}) || v == nil || v.Index < 1 || v.Index > protocol.MaxValidators || indexes[v.Index] || v.VoteAddress == (types.BLSPublicKey{}) || keys[v.VoteAddress] {
			return nil, errors.New("invalid snapshot validators")
		}
		indexes[v.Index] = true
		keys[v.VoteAddress] = true
	}
	// The exact Bohr sentinels differ at S/C. They are not ordinary heights.
	sentinel := ^uint64(0) - 1
	if n == 3072 {
		sentinel = ^uint64(0) - 3
	}
	for height, addr := range s.Recents {
		if height == sentinel {
			if addr != (common.Address{}) {
				return nil, errors.New("invalid Bohr sentinel")
			}
			continue
		}
		if (height != n-1 && height != n) || s.Validators[addr] == nil {
			return nil, errors.New("invalid recent signer")
		}
	}
	if addr, ok := s.Recents[sentinel]; !ok || addr != (common.Address{}) {
		return nil, errors.New("missing Bohr sentinel")
	}
	for height, hash := range s.RecentForkHashes {
		if height < n-95 || height > n || hash != "00000000" {
			return nil, errors.New("invalid recent fork history")
		}
	}
	vote := s.Attestation
	if vote.SourceNumber != n-2 || vote.TargetNumber != n-1 || vote.SourceHash != headers[n-2].Hash() || vote.TargetHash != headers[n-1].Hash() {
		return nil, errors.New("snapshot attestation mismatch")
	}
	return &s, nil
}

type chain struct {
	cfg           *params.ChainConfig
	head          *types.Header
	byNumber      map[uint64]*types.Header
	byHash        map[common.Hash]*types.Header
	reads, misses int
}

func (c *chain) Config() *params.ChainConfig  { return c.cfg }
func (c *chain) CurrentHeader() *types.Header { return c.head }
func (c *chain) GetHeader(hash common.Hash, n uint64) *types.Header {
	c.reads++
	h := c.byHash[hash]
	if h == nil || h.Number.Uint64() != n {
		c.misses++
		return nil
	}
	return h
}
func (c *chain) GetHeaderByNumber(n uint64) *types.Header           { return c.byNumber[n] }
func (c *chain) GetHeaderByHash(h common.Hash) *types.Header        { return c.byHash[h] }
func (c *chain) GenesisHeader() *types.Header                       { return c.byNumber[0] }
func (c *chain) GetTd(common.Hash, uint64) *big.Int                 { return nil }
func (c *chain) GetHighestVerifiedHeader() *types.Header            { return c.head }
func (c *chain) GetVerifiedBlockByHash(h common.Hash) *types.Header { return c.byHash[h] }
func (c *chain) add(h *types.Header) {
	c.byNumber[h.Number.Uint64()] = h
	c.byHash[h.Hash()] = h
	if c.head == nil || c.head.Number.Cmp(h.Number) < 0 {
		c.head = h
	}
}

func verify(c *chain, all []*types.Header, snaps map[uint64]*parlia.Snapshot, h *types.Header, req protocol.Request, result protocol.Response) protocol.Response {
	result.Stage = "restore"
	db := rawdb.NewMemoryDatabase()
	defer db.Close()
	if !req.Replay {
		for _, n := range req.Snapshots {
			s := snaps[n]
			raw, err := json.Marshal(s)
			if err != nil {
				result.Error = err.Error()
				return result
			}
			if err = db.Put(append([]byte("parlia-"), s.Hash[:]...), raw); err != nil {
				result.Error = err.Error()
				return result
			}
		}
	}
	p := parlia.New(c.cfg, db, nil, c.GenesisHeader().Hash())
	defer p.Close()
	if p.VotePool != nil {
		result.Error = "unexpected vote pool"
		return result
	}
	if req.Replay {
		result.Stage = "replay"
		for _, header := range all[1:protocol.CandidateNumber] {
			if err := p.VerifyHeader(c, header); err != nil {
				result.Error = err.Error()
				return result
			}
			c.add(header)
			result.VerifiedHeaders++
		}
	}
	result.CandidateVisibleBeforeVerify = c.byHash[h.Hash()] != nil || c.byNumber[protocol.CandidateNumber] != nil
	if result.CandidateVisibleBeforeVerify {
		result.Error = "candidate leaked into reader"
		return result
	}
	result.Stage = "verify"
	if err := p.VerifyHeader(c, h); err != nil {
		result.Error = err.Error()
		result.Reads = c.reads
		result.Misses = c.misses
		return result
	}
	c.add(h)
	result.VerifiedHeaders++ // Verify before reader admission.
	result.Stage = "postverify"
	s, err := p.APIs(c)[0].Service.(*parlia.API).GetSnapshotAtHash(h.Hash())
	if err != nil {
		result.Error = err.Error()
		return result
	}
	raw, err := json.Marshal(s)
	if err != nil {
		result.Error = err.Error()
		return result
	}
	justified, jhash, err := p.GetJustifiedNumberAndHash(c, []*types.Header{h})
	if err != nil {
		result.Error = err.Error()
		return result
	}
	finalized := p.GetFinalizedHeader(c, h)
	if finalized == nil || jhash != h.ParentHash || justified != 3072 || finalized.Number.Uint64() != 3071 {
		result.Error = "unexpected synthetic finality result"
		return result
	}
	// Set success-only fields together, after every verification step succeeds.
	result.Accepted = true
	result.Stage = "accepted"
	result.CandidateHash = h.Hash().Hex()
	result.SnapshotDigest = protocol.Digest(raw)
	result.Justified = justified
	result.Finalized = finalized.Number.Uint64()
	result.Reads = c.reads
	result.Misses = c.misses
	return result
}
