// SPDX-License-Identifier: GPL-3.0-or-later
package checkpointproof

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/rlp"
)

// This is a fixture limitation, not a requirement that real BSC headers fail.
func TestSyntheticHeaderWireBoundary(t *testing.T) {
	f := makeFixture(t)
	h := f.next(t, 1)
	if err := f.engine.VerifyHeader(f.chain, h); err != nil {
		t.Fatal(err)
	}
	for name, input := range map[string]*types.Header{"genesis": f.chain.GenesisHeader(), "successor": h} {
		t.Run(name, func(t *testing.T) {
			if input.ParentBeaconRoot == nil || input.WithdrawalsHash != nil {
				t.Fatal("fixture no longer has the reviewed optional-field gap")
			}
			wire, err := rlp.EncodeToBytes(input)
			if err != nil {
				t.Fatal(err)
			}
			var decoded types.Header
			err = rlp.DecodeBytes(wire, &decoded)
			if err == nil || !strings.Contains(err.Error(), "WithdrawalsHash") {
				t.Fatalf("expected fixture RLP gap rejection, got %v", err)
			}
			t.Logf("official RLP rejection: %v", err)
			j, err := json.Marshal(input)
			if err != nil {
				t.Fatal(err)
			}
			var restored types.Header
			if err := json.Unmarshal(j, &restored); err != nil {
				t.Fatal(err)
			}
			if restored.Hash() != input.Hash() {
				t.Fatal("official JSON changed fixture hash")
			}
			if name == "successor" {
				if err := f.engine.VerifyHeader(f.chain, &restored); err != nil {
					t.Fatal(err)
				}
			}
		})
	}
}
