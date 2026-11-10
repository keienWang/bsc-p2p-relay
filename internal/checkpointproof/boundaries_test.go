// SPDX-License-Identifier: GPL-3.0-or-later
package checkpointproof

import (
	"bytes"
	"encoding/json"
	"testing"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/consensus/parlia"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/crypto"
	"github.com/ethereum/go-ethereum/rlp"
	"github.com/prysmaticlabs/prysm/v5/crypto/bls"
)

func TestNonAdjacentAttestationAccumulatesFinality(t *testing.T) {
	f := makeFixture(t)
	f.advance(t, 20)
	before := snapshot(t, f.engine, f.chain, f.chain.head)
	if before.Attestation == nil {
		t.Fatal("fixture missing attestation")
	}
	old := *before.Attestation
	for f.chain.head.Number.Uint64() < 24 {
		h := f.next(t, 0)
		if err := f.engine.VerifyHeader(f.chain, h); err != nil {
			t.Fatal(err)
		}
		f.chain.add(h)
	}
	h := f.next(t, 3)
	var vote types.VoteAttestation
	if err := rlp.DecodeBytes(h.Extra[32:len(h.Extra)-65], &vote); err != nil {
		t.Fatal(err)
	}
	if vote.Data.SourceNumber+1 == vote.Data.TargetNumber || vote.Data.SourceNumber == old.SourceNumber {
		t.Fatal("fixture not non-adjacent")
	}
	if err := f.engine.VerifyHeader(f.chain, h); err != nil {
		t.Fatal(err)
	}
	f.chain.add(h)
	got := snapshot(t, f.engine, f.chain, h)
	if got.Attestation.SourceNumber != old.SourceNumber || got.Attestation.SourceHash != old.SourceHash || got.Attestation.TargetNumber != vote.Data.TargetNumber || got.Attestation.TargetHash != vote.Data.TargetHash {
		t.Fatal("accumulated finality is incorrect")
	}
	if *got.Attestation == *vote.Data {
		t.Fatal("snapshot state replaced by latest vote data")
	}
	final := f.engine.GetFinalizedHeader(f.chain, h)
	if final == nil || final.Number.Uint64() != old.SourceNumber || final.Hash() != old.SourceHash {
		t.Fatal("finality advanced on non-adjacent vote")
	}
}

func TestPrivateSnapshotLoaderTrustsFields(t *testing.T) {
	f := makeFixture(t)
	f.advanceWithTarget(t, 1024, 0)
	h := f.chain.head
	good := snapshot(t, f.engine, f.chain, h)
	var forged parlia.Snapshot
	if err := json.Unmarshal(encoded(t, good), &forged); err != nil {
		t.Fatal(err)
	}
	forged.Number = 999
	forged.Hash = common.Hash{1}
	forged.EpochLength = 0
	forged.BlockInterval = 0
	forged.TurnLength = 0
	c := f.chain.slice(1024, 1024)
	p, db := fresh(t, c)
	if err := db.Put(append([]byte("parlia-"), h.Hash().Bytes()...), encoded(t, &forged)); err != nil {
		t.Fatal(err)
	}
	got := snapshot(t, p, c, h)
	if got.Number != 999 || got.Hash != forged.Hash {
		t.Fatal("upstream loader behavior changed; review boundary")
	}
	if got.EpochLength != 200 || got.BlockInterval != 3000 || got.TurnLength != 1 {
		t.Fatal("upstream default behavior changed; review boundary")
	}
	t.Logf("loader trusted mismatched number/hash, defaulted epoch=%d interval=%d turn=%d", got.EpochLength, got.BlockInterval, got.TurnLength)
	var wire map[string]json.RawMessage
	if err := json.Unmarshal(encoded(t, good), &wire); err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{"number", "hash", "epoch_length", "block_interval", "turn_length", "validators", "recents", "recent_fork_hashes", "attestation:omitempty"} {
		if _, ok := wire[key]; !ok {
			t.Fatal("missing key", key)
		}
	}
	if len(wire) != 9 {
		t.Fatal("upstream codec fields changed")
	}
	var validators map[string]map[string]json.RawMessage
	if err := json.Unmarshal(wire["validators"], &validators); err != nil {
		t.Fatal(err)
	}
	for _, v := range validators {
		if len(v) != 2 || !bytes.HasPrefix(v["vote_address"], []byte("[")) {
			t.Fatal("key codec changed")
		}
		var key []uint64
		if err := json.Unmarshal(v["vote_address"], &key); err != nil || len(key) != 48 {
			t.Fatal("wrong key encoding")
		}
	}
}

func TestBLSQuorumBoundaries(t *testing.T) {
	f := makeFixture(t)
	f.advance(t, 5)
	original := f.next(t, 1)
	parent := snapshot(t, f.engine, f.chain, f.chain.head)
	for _, n := range []int{0, 1, 2, 3} {
		h := types.CopyHeader(original)
		var a types.VoteAttestation
		if err := rlp.DecodeBytes(h.Extra[32:len(h.Extra)-65], &a); err != nil {
			t.Fatal(err)
		}
		a.VoteAddressSet = types.ValidatorsBitSet((1 << n) - 1)
		a.AggSignature = types.BLSSignature{}
		if n > 0 {
			hash := a.Data.Hash()
			sigs := make([]bls.Signature, n)
			for i := range sigs {
				sigs[i] = f.votes[i].Sign(hash[:])
			}
			copy(a.AggSignature[:], bls.AggregateSignatures(sigs).Marshal())
		}
		h.Extra = f.extra(false, &a)
		key := f.keys[(h.Number.Uint64()/uint64(parent.TurnLength))%uint64(len(f.keys))]
		sig, err := crypto.Sign(types.SealHash(h, f.chain.cfg.ChainID).Bytes(), key)
		if err != nil {
			t.Fatal(err)
		}
		copy(h.Extra[len(h.Extra)-65:], sig)
		p, _ := fresh(t, f.chain)
		err = p.VerifyHeader(f.chain, h)
		if (err == nil) != (n >= 2) {
			t.Fatalf("%d of 3 quorum: %v", n, err)
		}
	}
}

// Upstream snapshot replay assumes historical votes were already verified.
// This intentionally exposes an invalid candidate ONLY to a test reader to
// demonstrate why a future adapter must never do so before VerifyHeader.
func TestSnapshotReplayDoesNotVerifyBLS(t *testing.T) {
	f := makeFixture(t)
	f.advance(t, 5)
	h := f.next(t, 1)
	var a types.VoteAttestation
	if err := rlp.DecodeBytes(h.Extra[32:len(h.Extra)-65], &a); err != nil {
		t.Fatal(err)
	}
	a.AggSignature[0] ^= 1
	h.Extra = f.extra(false, &a)
	parent := snapshot(t, f.engine, f.chain, f.chain.head)
	key := f.keys[(h.Number.Uint64()/uint64(parent.TurnLength))%uint64(len(f.keys))]
	sig, err := crypto.Sign(types.SealHash(h, f.chain.cfg.ChainID).Bytes(), key)
	if err != nil {
		t.Fatal(err)
	}
	copy(h.Extra[len(h.Extra)-65:], sig)
	if err := f.engine.VerifyHeader(f.chain, h); err == nil {
		t.Fatal("invalid aggregate accepted by VerifyHeader")
	}
	unsafe := f.chain.slice(1, 5)
	unsafe.add(h)
	p, _ := fresh(t, unsafe)
	replayed := snapshot(t, p, unsafe, h)
	if replayed.Attestation == nil || *replayed.Attestation != *a.Data {
		t.Fatal("snapshot application behavior changed; reassess trust boundary")
	}
	t.Log("snapshot replay consumed invalid BLS data: authenticated historical suffix and verify-before-reader ordering are mandatory")
}
