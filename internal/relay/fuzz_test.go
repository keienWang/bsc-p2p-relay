// SPDX-License-Identifier: GPL-3.0-or-later
package relay

import (
	"bytes"
	"fmt"
	"math/big"
	"testing"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/crypto"
)

// validationSeeds uses a publicly predictable test-only key for reproducible
// fixtures. These zero-value transactions never leave this offline test suite.
// The key must never be used for funds or for an actual network account.
func validationSeeds(tb testing.TB) [][]byte {
	tb.Helper()
	key, err := crypto.ToECDSA(bytes.Repeat([]byte{1}, 32))
	if err != nil {
		tb.Fatal(err)
	}
	chainID := big.NewInt(56)
	recipient := common.Address{19: 1}
	fixtures := []*types.Transaction{
		types.NewTx(&types.LegacyTx{Nonce: 1, To: &recipient, Gas: 21000, GasPrice: big.NewInt(1)}),
		types.NewTx(&types.AccessListTx{ChainID: chainID, Nonce: 2, To: &recipient, Gas: 21000, GasPrice: big.NewInt(1)}),
		types.NewTx(&types.DynamicFeeTx{ChainID: chainID, Nonce: 3, To: &recipient, Gas: 21000, GasTipCap: big.NewInt(1), GasFeeCap: big.NewInt(2)}),
	}
	var result [][]byte
	for _, tx := range fixtures {
		signed, err := types.SignTx(tx, types.LatestSignerForChainID(chainID), key)
		if err != nil {
			tb.Fatal(err)
		}
		raw, err := signed.MarshalBinary()
		if err != nil {
			tb.Fatal(err)
		}
		if _, _, err := validate(raw, chainID); err != nil {
			tb.Fatalf("invalid valid-seed fixture: %v", err)
		}
		result = append(result, raw)
	}
	return result
}

func FuzzValidateRawTransaction(f *testing.F) {
	for _, raw := range [][]byte{
		{}, {0x00}, {0xc0}, {0x01}, {0x02, 0xc0}, {0x03, 0xc0},
		{0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff},
		bytes.Repeat([]byte{0xff}, MaxRawTransaction),
		bytes.Repeat([]byte{0xff}, MaxRawTransaction+1),
	} {
		f.Add(raw)
	}
	for _, raw := range validationSeeds(f) {
		f.Add(raw)
		f.Add(bytes.Clone(raw[:len(raw)-1]))  // Truncated signature or envelope.
		f.Add(append(bytes.Clone(raw), 0x00)) // Noncanonical trailing bytes.
	}
	f.Fuzz(func(t *testing.T, raw []byte) {
		// Exercise the size limit itself, but avoid spending fuzz-worker resources
		// on arbitrarily large inputs rejected before decoding.
		if len(raw) > MaxRawTransaction+1 {
			return
		}
		chainID := big.NewInt(56)
		tx, sender, err := validate(raw, chainID)
		if err != nil {
			return
		}
		if tx == nil {
			t.Fatal("successful validation returned nil transaction")
		}
		if len(raw) == 0 || len(raw) > MaxRawTransaction {
			t.Fatal("accepted invalid input size")
		}
		if chainID.Cmp(big.NewInt(56)) != 0 {
			t.Fatal("validator mutated configured chain ID")
		}
		if !tx.Protected() || tx.ChainId().Cmp(chainID) != 0 {
			t.Fatal("accepted unprotected or wrong-chain transaction")
		}
		switch tx.Type() {
		case types.LegacyTxType, types.AccessListTxType, types.DynamicFeeTxType:
		default:
			t.Fatalf("accepted unsupported type %d", tx.Type())
		}
		canonical, err := tx.MarshalBinary()
		if err != nil || !bytes.Equal(canonical, raw) {
			t.Fatalf("accepted noncanonical encoding: %v", err)
		}
		recovered, err := types.Sender(types.LatestSignerForChainID(chainID), tx)
		if err != nil || recovered != sender {
			t.Fatalf("inconsistent sender recovery: %v", err)
		}
		if tx.Gas() < 21000 || tx.GasTipCap().Cmp(tx.GasFeeCap()) > 0 {
			t.Fatal("accepted invalid gas/fee limits")
		}
		if tx.Value().BitLen() > 256 || tx.GasFeeCap().BitLen() > 256 || tx.GasTipCap().BitLen() > 256 {
			t.Fatal("accepted uint256 overflow")
		}
	})
}

func BenchmarkValidateRawTransaction(b *testing.B) {
	for typ, raw := range validationSeeds(b) {
		b.Run(fmt.Sprintf("type_%d", typ), func(b *testing.B) {
			chainID := big.NewInt(56)
			b.ReportAllocs()
			b.SetBytes(int64(len(raw)))
			b.ResetTimer()
			for i := 0; i < b.N; i++ {
				if _, _, err := validate(raw, chainID); err != nil {
					b.Fatal(err)
				}
			}
		})
	}
}
