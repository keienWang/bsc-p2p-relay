// SPDX-License-Identifier: GPL-3.0-or-later
package checkpointproof

import (
	"bytes"
	"testing"

	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/rlp"
	"github.com/prysmaticlabs/prysm/v5/crypto/bls"
)

func TestModernWrongMessageReachesCrypto(t *testing.T) {
	f := makeModernWireFixture(t)
	f.advance(t, 12)
	h := canonicalWireHeader(t, f.next(t, 1))
	var vote types.VoteAttestation
	if err := rlp.DecodeBytes(h.Extra[32:len(h.Extra)-65], &vote); err != nil {
		t.Fatal(err)
	}
	correct := vote.Data.Hash()
	wrong := correct
	wrong[0] ^= 1
	var pubs []bls.PublicKey
	var signatures []bls.Signature
	for _, key := range f.votes {
		pubs = append(pubs, key.PublicKey())
		signatures = append(signatures, key.Sign(wrong[:]))
	}
	original, err := bls.SignatureFromBytes(vote.AggSignature[:])
	if err != nil || !original.FastAggregateVerify(pubs, correct) {
		t.Fatalf("original aggregate invalid: %v", err)
	}
	copy(vote.AggSignature[:], bls.AggregateSignatures(signatures).Marshal())
	aggregate, err := bls.SignatureFromBytes(vote.AggSignature[:])
	if err != nil {
		t.Fatal(err)
	}
	if !aggregate.FastAggregateVerify(pubs, wrong) || aggregate.FastAggregateVerify(pubs, correct) {
		t.Fatal("wrong-message cryptographic control failed")
	}
	bad := types.CopyHeader(h)
	bad.Extra = f.extra(false, &vote)
	f.sign(t, bad)
	bad = canonicalWireHeader(t, bad)
	if err := f.engine.VerifyHeader(f.chain, bad); err == nil || err.Error() != "invalid attestation, signature verify failed" {
		t.Fatalf("did not reach BLS aggregate verification: %v", err)
	}
}

func TestModernRejectNoncanonicalWire(t *testing.T) {
	f := makeModernWireFixture(t)
	h := canonicalWireHeader(t, f.next(t, 1))
	wire, err := rlp.EncodeToBytes(h)
	if err != nil {
		t.Fatal(err)
	}
	var fields []rlp.RawValue
	if err := rlp.DecodeBytes(wire, &fields); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(fields[8], []byte{1}) {
		t.Fatal("fixture number encoding changed")
	}
	fields[8] = rlp.RawValue{0x81, 0x01}
	nonminimalNumber, err := rlp.EncodeToBytes(fields)
	if err != nil {
		t.Fatal(err)
	}
	if wire[0] != 0xf9 {
		t.Fatalf("expected two-byte list length, got %x", wire[:3])
	}
	nonminimalLength := append([]byte{0xfa, 0}, wire[1:]...)
	for name, bad := range map[string][]byte{"trailing": append(append([]byte(nil), wire...), 0x80), "nonminimal_number": nonminimalNumber, "nonminimal_list_length": nonminimalLength, "truncated": wire[:len(wire)-1]} {
		t.Run(name, func(t *testing.T) {
			var decoded types.Header
			if err := rlp.DecodeBytes(bad, &decoded); err == nil {
				t.Fatal("noncanonical or incomplete RLP accepted")
			} else {
				t.Log(err)
			}
		})
	}
}
