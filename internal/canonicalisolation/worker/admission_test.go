//go:build canonicalisolation

// SPDX-License-Identifier: GPL-3.0-or-later
package main

import (
	"bytes"
	"encoding/json"
	"math/big"
	"os"
	"path/filepath"
	"testing"

	"github.com/ethereum/go-ethereum/consensus/parlia"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/rlp"
	"github.com/keienWang/bsc-p2p-relay/internal/canonicalisolation/protocol"
)

func TestCanonicalDecoder(t *testing.T) {
	// No engine/BLS operation. A small synthetic header suffices for wire rules.
	h := &types.Header{Number: big.NewInt(1), Difficulty: big.NewInt(1)}
	wire, err := rlp.EncodeToBytes(h)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = decodeHeader(wire); err != nil {
		t.Fatal(err)
	}
	var fields []rlp.RawValue
	if err = rlp.DecodeBytes(wire, &fields); err != nil {
		t.Fatal(err)
	}
	fields[8] = rlp.RawValue{0x81, 0x01}
	nonminimal, err := rlp.EncodeToBytes(fields)
	if err != nil {
		t.Fatal(err)
	}
	if wire[0] != 0xf9 {
		t.Fatalf("unexpected header RLP length prefix %x", wire[0])
	}
	for name, raw := range map[string][]byte{
		"trailing": append(bytes.Clone(wire), 0x80), "truncated": wire[:len(wire)-1],
		"nonminimal_integer": nonminimal, "nonminimal_length": append([]byte{0xfa, 0}, wire[1:]...),
		"oversize": make([]byte, protocol.MaxRecord+1),
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := decodeHeader(raw); err == nil {
				t.Fatal("bad wire accepted")
			}
		})
	}
}

func fixtureBundle(t *testing.T) protocol.Bundle {
	t.Helper()
	dir := os.Getenv("BSC_CANONICAL_FIXTURE_DIR")
	if dir == "" {
		t.Fatal("set BSC_CANONICAL_FIXTURE_DIR to independently reproduced modern-wire artifacts")
	}
	read := func(name string) []byte {
		b, err := os.ReadFile(filepath.Join(dir, name))
		if err != nil {
			t.Fatal(err)
		}
		return b
	}
	return protocol.Bundle{Config: read("config.json"), Headers: read("headers.rlp"), Support: read("snapshot-2048.json"), Anchor: read("snapshot-3072.json")}
}

func TestSnapshotStrictSemantics(t *testing.T) {
	bundle := fixtureBundle(t)
	_, headers, _, err := decodeBundle(bundle)
	if err != nil {
		t.Fatal(err)
	}
	// Marshal typed upstream snapshots so semantic failures cannot be masked
	// by a map encoder changing the canonical field order first.
	for name, tc := range map[string]struct {
		mutate func(*parlia.Snapshot)
		want   string
	}{
		"zero_epoch":       {func(s *parlia.Snapshot) { s.EpochLength = 0 }, "snapshot profile mismatch"},
		"null_validators":  {func(s *parlia.Snapshot) { s.Validators = nil }, "snapshot profile mismatch"},
		"wrong_number":     {func(s *parlia.Snapshot) { s.Number = 3072 }, "snapshot profile mismatch"},
		"missing_sentinel": {func(s *parlia.Snapshot) { delete(s.Recents, ^uint64(0)-1) }, "snapshot profile mismatch"},
		"duplicate_index": {func(s *parlia.Snapshot) {
			for _, v := range s.Validators {
				v.Index = 1
			}
		}, "invalid snapshot validators"},
		"zero_vote_key": {func(s *parlia.Snapshot) {
			for _, v := range s.Validators {
				v.VoteAddress = types.BLSPublicKey{}
			}
		}, "invalid snapshot validators"},
		"wrong_fork_hash":   {func(s *parlia.Snapshot) { s.RecentForkHashes[2048] = "01000000" }, "invalid recent fork history"},
		"wrong_vote_source": {func(s *parlia.Snapshot) { s.Attestation.SourceNumber++ }, "snapshot attestation mismatch"},
	} {
		t.Run(name, func(t *testing.T) {
			var snapshot parlia.Snapshot
			if err := json.Unmarshal(bundle.Support, &snapshot); err != nil {
				t.Fatal(err)
			}
			tc.mutate(&snapshot)
			raw, err := json.Marshal(&snapshot)
			if err != nil {
				t.Fatal(err)
			}
			if _, err = decodeSnapshot(raw, 2048, headers); err == nil || err.Error() != tc.want {
				t.Fatalf("wrong semantic rejection: %v, want %q", err, tc.want)
			}
		})
	}
	for name, raw := range map[string][]byte{
		"missing_epoch":              bytes.Replace(bundle.Support, []byte(`"epoch_length":1000,`), nil, 1),
		"normalized_attestation_tag": bytes.Replace(bundle.Support, []byte(`"attestation:omitempty"`), []byte(`"attestation"`), 1),
		"normalized_index_tag":       bytes.Replace(bundle.Support, []byte(`"index:omitempty"`), []byte(`"index"`), 1),
		"short_vote_key":             bytes.Replace(bundle.Support, []byte(`"vote_address":[128,253,117,235`), []byte(`"vote_address":[128`), 1),
		"case_alias":                 bytes.Replace(bundle.Support, []byte(`"epoch_length"`), []byte(`"EPOCH_LENGTH"`), 1),
		"leading_zero_map_key":       bytes.Replace(bundle.Support, []byte(`"2048":`), []byte(`"02048":`), 1),
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := decodeSnapshot(raw, 2048, headers); err == nil {
				t.Fatal("noncanonical snapshot JSON accepted")
			}
		})
	}
	// Duplicate-key rejection is not masked by map reserialization.
	raw := bytes.Replace(bundle.Support, []byte(`"number":2048`), []byte(`"number":1,"number":2048`), 1)
	if _, err = decodeSnapshot(raw, 2048, headers); err == nil {
		t.Fatal("duplicate snapshot key accepted")
	}
}

func TestAuthenticatedBundleLimits(t *testing.T) {
	good := fixtureBundle(t)
	if err := good.Authenticate(); err != nil {
		t.Fatal(err)
	}
	for name, mutate := range map[string]func(*protocol.Bundle){
		"config":  func(b *protocol.Bundle) { b.Config = make([]byte, protocol.MaxRecord+1) },
		"headers": func(b *protocol.Bundle) { b.Headers = make([]byte, protocol.HeaderBytes+1) },
		"support": func(b *protocol.Bundle) { b.Support = make([]byte, protocol.MaxRecord+1) },
		"anchor":  func(b *protocol.Bundle) { b.Anchor = make([]byte, protocol.MaxRecord+1) },
	} {
		t.Run(name, func(t *testing.T) {
			// All untouched fields remain exact authenticated bytes, irrespective of
			// iteration order in Bundle.Authenticate's map.
			bad := good
			mutate(&bad)
			if err := bad.Authenticate(); err == nil || err.Error() != "bundle record size exceeds bound" {
				t.Fatalf("wrong bound rejection: %v", err)
			}
		})
	}
}
