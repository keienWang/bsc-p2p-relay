// SPDX-License-Identifier: GPL-3.0-or-later

package firstepoch

import (
	"bytes"
	"encoding/json"
	"os"
	"testing"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/rlp"
	"github.com/ethereum/go-ethereum/trie"
	"github.com/keienWang/bsc-p2p-relay/internal/evidence"
)

const realBlockTwoHash = "0xe9296d312a937cdefc201a8fe80dbfa8a9c958ada9e863ddbb324804722f1de0"
const realBlockTwoTxHash = "0x1d06a9d52255a2a4385d55093aec7671f3d7f6d83d4cd438991be8b6588e9b91"

func readRealHeader(t *testing.T, name string) *types.Header {
	t.Helper()
	b, err := os.ReadFile("testdata/" + name)
	if err != nil {
		t.Fatal(err)
	}
	var h types.Header
	if err := json.Unmarshal(b, &h); err != nil {
		t.Fatal(err)
	}
	return &h
}

func realChainThroughOne(t *testing.T) *Verifier {
	t.Helper()
	b, err := os.ReadFile("testdata/mainnet-genesis.rlp")
	if err != nil {
		t.Fatal(err)
	}
	v, err := New(56, b, MaxHeaders)
	if err != nil {
		t.Fatal(err)
	}
	h := readRealHeader(t, "mainnet-block1.json")
	if err := official(v, h); err != nil {
		t.Fatal(err)
	}
	if _, err := v.Append(encode(t, h)); err != nil {
		t.Fatal(err)
	}
	return v
}

func TestGenuineMainnetBlockTwo(t *testing.T) {
	v := realChainThroughOne(t)
	h := readRealHeader(t, "mainnet-block2.json")
	// Constants also occur in the independent, pinned ethers-io block-2 fixture.
	// Neither a remote JSON hash nor remote parent/TD metadata establishes trust.
	if h.Hash() != common.HexToHash(realBlockTwoHash) {
		t.Fatalf("block 2 hash %s", h.Hash())
	}
	if h.ParentHash != v.Tip().Hash() {
		t.Fatal("block 2 parent is not authenticated block 1")
	}
	if err := official(v, h); err != nil {
		t.Fatal(err)
	}
	got, err := v.Append(encode(t, h))
	if err != nil {
		t.Fatal(err)
	}
	if got.Hash() != h.Hash() || v.Tip().Hash() != h.Hash() {
		t.Fatal("incorrect authenticated tip")
	}
}

func TestGenuineBlockTwoTampering(t *testing.T) {
	for _, field := range []string{"state_root", "tx_root", "receipt_root", "bloom", "seal", "parent"} {
		t.Run(field, func(t *testing.T) {
			v := realChainThroughOne(t)
			before := v.Tip().Hash()
			h := readRealHeader(t, "mainnet-block2.json")
			switch field {
			case "state_root":
				h.Root[0] ^= 1
			case "tx_root":
				h.TxHash[0] ^= 1
			case "receipt_root":
				h.ReceiptHash[0] ^= 1
			case "bloom":
				h.Bloom[0] ^= 1
			case "seal":
				h.Extra[len(h.Extra)-1] ^= 1
			case "parent":
				h.ParentHash[0] ^= 1
			}
			if err := official(v, h); err == nil {
				t.Fatal("official engine accepted mutation")
			}
			if got, err := v.Append(encode(t, h)); err == nil || got != nil {
				t.Fatal("append accepted mutation")
			}
			if v.Tip().Hash() != before {
				t.Fatal("mutation advanced tip")
			}
		})
	}
}

func TestGenuineBlockTwoTransactionCommitment(t *testing.T) {
	v := realChainThroughOne(t)
	h, err := v.Append(encode(t, readRealHeader(t, "mainnet-block2.json")))
	if err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile("testdata/mainnet-block2-transactions.json")
	if err != nil {
		t.Fatal(err)
	}
	var txs types.Transactions
	if err := json.Unmarshal(data, &txs); err != nil {
		t.Fatal(err)
	}
	if len(txs) != 1 {
		t.Fatal("expected source's complete single-transaction list")
	}
	tx := txs[0]
	if tx.Type() != types.LegacyTxType || tx.ChainId().Uint64() != 56 || tx.Hash() != common.HexToHash(realBlockTwoTxHash) {
		t.Fatal("transaction identity mismatch")
	}
	// Preserve the existing signature; never sign, broadcast, or execute this tx.
	canonical, err := tx.MarshalBinary()
	if err != nil {
		t.Fatal(err)
	}
	var decoded types.Transaction
	if err := decoded.UnmarshalBinary(canonical); err != nil {
		t.Fatal(err)
	}
	roundtrip, err := decoded.MarshalBinary()
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(canonical, roundtrip) || decoded.Hash() != tx.Hash() {
		t.Fatal("noncanonical transaction round trip")
	}
	root := types.DeriveSha(txs, trie.NewStackTrie(nil))
	if root != h.TxHash {
		t.Fatalf("transaction root %s != authenticated commitment %s", root, h.TxHash)
	}
	list, err := rlp.EncodeToBytes(txs)
	if err != nil {
		t.Fatal(err)
	}
	var roundtripList types.Transactions
	if err := rlp.DecodeBytes(list, &roundtripList); err != nil {
		t.Fatal(err)
	}
	if types.DeriveSha(roundtripList, trie.NewStackTrie(nil)) != h.TxHash {
		t.Fatal("list encoding changed commitment")
	}
	// Changed payload, missing transaction and duplicate transaction all break the
	// commitment. This tests list completeness, not normal-tx gas/execution rules.
	to := tx.To()
	vv, r, s := tx.RawSignatureValues()
	changedData := append([]byte{}, tx.Data()...)
	changedData[0] ^= 1
	mutated := types.NewTx(&types.LegacyTx{Nonce: tx.Nonce(), GasPrice: tx.GasPrice(), Gas: tx.Gas(), To: to, Value: tx.Value(), Data: changedData, V: vv, R: r, S: s})
	for _, bad := range []types.Transactions{nil, {mutated}, {tx, tx}} {
		if types.DeriveSha(bad, trie.NewStackTrie(nil)) == h.TxHash {
			t.Fatal("invalid transaction list matched commitment")
		}
	}
	// There are no receipt details in this source. Neither omitted nor fabricated
	// empty receipts may produce an outcome result from the existing evidence API.
	for _, receipts := range [][]byte{nil, {0xc0}} {
		if result, err := evidence.CheckCompleteLists(h, list, receipts); err == nil || result != nil {
			t.Fatal("missing receipts produced an outcome")
		}
	}
}
