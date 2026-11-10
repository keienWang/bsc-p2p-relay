// SPDX-License-Identifier: GPL-3.0-or-later
package firstepoch

import (
	"encoding/json"
	"fmt"
	"math/big"
	"testing"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/crypto"
	"github.com/ethereum/go-ethereum/params"
)

func TestEachOptionalFieldRejected(t *testing.T) {
	fields := map[string]func(*types.Header){
		"basefee":     func(h *types.Header) { h.BaseFee = new(big.Int) },
		"withdrawals": func(h *types.Header) { h.WithdrawalsHash = new(common.Hash) },
		"blobgas":     func(h *types.Header) { h.BlobGasUsed = new(uint64) },
		"excess":      func(h *types.Header) { h.ExcessBlobGas = new(uint64) },
		"beacon":      func(h *types.Header) { h.ParentBeaconRoot = new(common.Hash) },
		"requests":    func(h *types.Header) { h.RequestsHash = new(common.Hash) },
		"accesslist":  func(h *types.Header) { h.BlockAccessListHash = new(common.Hash) },
		"slot":        func(h *types.Header) { h.SlotNumber = new(uint64) },
	}
	for name, mutate := range fields {
		t.Run(name, func(t *testing.T) {
			v, keys := fixture(t, 3)
			h := signed(t, v, keys)
			before := v.Tip().Hash()
			mutate(h)
			if got, err := v.Append(encode(t, h)); err == nil || got != nil {
				t.Fatal("optional field accepted")
			}
			if v.Tip().Hash() != before {
				t.Fatal("failed candidate changed tip")
			}
		})
	}
}

func TestConfigAndInputIsolation(t *testing.T) {
	v, keys := fixture(t, 4)
	b, _ := json.Marshal(params.BSCChainConfig)
	var cfg params.ChainConfig
	if err := json.Unmarshal(b, &cfg); err != nil {
		t.Fatal(err)
	}
	genesis := v.Tip()
	v, err := newAnchored(&cfg, genesis, 4)
	if err != nil {
		t.Fatal(err)
	}
	cfg.ChainID.SetInt64(999)
	*cfg.ShanghaiTime = 0
	genesis.Number.SetInt64(111)
	genesis.Extra[0] ^= 1
	h := signed(t, v, keys)
	data := encode(t, h)
	want := h.Hash()
	got, err := v.Append(data)
	if err != nil {
		t.Fatal(err)
	}
	for i := range data {
		data[i] ^= 0xff
	}
	got.Number.SetInt64(42)
	got.Difficulty.SetInt64(999)
	got.Extra[0] ^= 1
	if v.Tip().Hash() != want || v.chain.cfg.ChainID.Uint64() != 56 || *v.chain.cfg.ShanghaiTime == 0 {
		t.Fatal("state aliases caller")
	}
	if _, err := v.Append(encode(t, signed(t, v, keys))); err != nil {
		t.Fatal(err)
	}
}

func TestUnauthorizedValidSeal(t *testing.T) {
	v, keys := fixture(t, 3)
	h := signed(t, v, keys)
	raw := make([]byte, 32)
	raw[31] = 77
	k, err := crypto.ToECDSA(raw)
	if err != nil {
		t.Fatal(err)
	}
	h.Coinbase = crypto.PubkeyToAddress(k.PublicKey)
	resign(t, v, h, k)
	if err := official(v, h); err == nil {
		t.Fatal("official accepted unauthorized signer")
	}
	if _, err := v.Append(encode(t, h)); err == nil {
		t.Fatal("accepted unauthorized valid seal")
	}
}

func TestHistoricalTimingByNetwork(t *testing.T) {
	for _, tc := range []struct {
		name    string
		config  *params.ChainConfig
		accepts bool
	}{
		{"mainnet", params.BSCChainConfig, false},
		{"chapel", params.ChapelChainConfig, true},
	} {
		for _, delta := range []uint64{0, 1} {
			t.Run(fmt.Sprintf("%s/parent-minus-%d", tc.name, delta), func(t *testing.T) {
				v, keys := fixture(t, 5)
				v, err := newAnchored(tc.config, v.Tip(), 5)
				if err != nil {
					t.Fatal(err)
				}
				h := signed(t, v, keys)
				h.Time = v.Tip().Time - delta
				resign(t, v, h, keys[1])
				// Historical Chapel had not activated Ramanujan. Mainnet had activated
				// it at genesis. Preserve those exact rules, not modern-time semantics.
				if accepted := official(v, h) == nil; accepted != tc.accepts {
					t.Fatalf("official acceptance %v, want %v", accepted, tc.accepts)
				}
				if _, err := v.Append(encode(t, h)); (err == nil) != tc.accepts {
					t.Fatalf("adapter result %v, want acceptance %v", err, tc.accepts)
				}
			})
		}
	}
}

func TestNumericAndExtraBounds(t *testing.T) {
	for _, tc := range []struct {
		name   string
		change func(*types.Header)
	}{
		{"number_overflow", func(h *types.Header) { h.Number = new(big.Int).Lsh(big.NewInt(1), 64) }},
		{"difficulty_overflow", func(h *types.Header) { h.Difficulty = new(big.Int).Lsh(big.NewInt(1), 256) }},
		{"missing_extra", func(h *types.Header) { h.Extra = nil }},
		{"oversized_extra", func(h *types.Header) { h.Extra = make([]byte, MaxHeaderBytes) }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			v, keys := fixture(t, 3)
			h := signed(t, v, keys)
			before := v.Tip().Hash()
			tc.change(h)
			if got, err := v.Append(encode(t, h)); err == nil || got != nil {
				t.Fatal("accepted malformed header")
			}
			if v.Tip().Hash() != before {
				t.Fatal("failed candidate changed tip")
			}
		})
	}
}
