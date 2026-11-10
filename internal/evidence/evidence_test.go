// SPDX-License-Identifier: GPL-3.0-or-later
package evidence

import (
	"bytes"
	"errors"
	"math/big"
	"testing"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/rlp"
	"github.com/ethereum/go-ethereum/trie"
)

func encode(t testing.TB, value any) []byte {
	t.Helper()
	b, err := rlp.EncodeToBytes(value)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

// These unsigned synthetic transactions never leave the test process. Signature
// validity is intentionally not implied by commitment verification.
func fixture(t testing.TB) (*types.Header, types.Transactions, types.Receipts) {
	t.Helper()
	txs := types.Transactions{
		types.NewTx(&types.LegacyTx{Nonce: 1, GasPrice: big.NewInt(0), Gas: 21000, Value: big.NewInt(0)}),
		types.NewTx(&types.AccessListTx{ChainID: big.NewInt(56), Nonce: 2, GasPrice: big.NewInt(1), Gas: 21000, Value: big.NewInt(0)}),
		types.NewTx(&types.DynamicFeeTx{ChainID: big.NewInt(56), Nonce: 3, GasTipCap: big.NewInt(1), GasFeeCap: big.NewInt(2), Gas: 21000, Value: big.NewInt(0)}),
	}
	rs := types.Receipts{
		&types.Receipt{Type: 0, Status: 1, CumulativeGasUsed: 0},
		&types.Receipt{Type: 1, Status: 0, CumulativeGasUsed: 21000},
		&types.Receipt{Type: 2, Status: 1, CumulativeGasUsed: 42000, Logs: []*types.Log{{Address: common.Address{1}, Topics: []common.Hash{{2}}, Data: []byte{3}}}},
	}
	for _, r := range rs {
		r.Bloom = types.CreateBloom(r)
	}
	h := &types.Header{Number: big.NewInt(100), Difficulty: big.NewInt(2), TxHash: types.DeriveSha(txs, trie.NewStackTrie(nil)), ReceiptHash: types.DeriveSha(rs, trie.NewStackTrie(nil))}
	return h, txs, rs
}

func TestConditionalMixedTypes(t *testing.T) {
	h, txs, rs := fixture(t)
	result, err := CheckCompleteLists(h, encode(t, txs), encode(t, rs))
	if err != nil {
		t.Fatal(err)
	}
	if result.HeaderAuthentication != "not_checked" || result.HeaderHash != h.Hash() || len(result.Entries) != 3 {
		t.Fatalf("unexpected result: %+v", result)
	}
	for i, entry := range result.Entries {
		if entry.Index != uint64(i) || entry.TransactionHash != txs[i].Hash() || entry.TransactionType != txs[i].Type() {
			t.Fatalf("entry %d: %+v", i, entry)
		}
	}
	if result.Entries[0].Outcome != OutcomeSuccess || result.Entries[1].Outcome != OutcomeFailure {
		t.Fatal("wrong outcomes")
	}
	// Equal/zero cumulative gas is deliberately not rejected: system transaction
	// gas semantics must not be inferred from ordinary transaction assumptions.
	if result.Entries[0].CumulativeGasUsed != 0 {
		t.Fatal("gas changed")
	}
}

func TestRejectedEvidence(t *testing.T) {
	tests := []struct {
		name   string
		change func(*types.Header, types.Transactions, types.Receipts) (*types.Header, []byte, []byte)
		want   error
	}{
		{"nil header", func(h *types.Header, tx types.Transactions, rs types.Receipts) (*types.Header, []byte, []byte) {
			return nil, encode(t, tx), encode(t, rs)
		}, ErrInput},
		{"nil list", func(h *types.Header, tx types.Transactions, rs types.Receipts) (*types.Header, []byte, []byte) {
			return h, nil, encode(t, rs)
		}, ErrInput},
		{"missing receipt", func(h *types.Header, tx types.Transactions, rs types.Receipts) (*types.Header, []byte, []byte) {
			return h, encode(t, tx), encode(t, rs[:2])
		}, ErrInput},
		{"receipt type", func(h *types.Header, tx types.Transactions, rs types.Receipts) (*types.Header, []byte, []byte) {
			rs[0].Type = 1
			return h, encode(t, tx), encode(t, rs)
		}, ErrInput},
		{"receipt mutation", func(h *types.Header, tx types.Transactions, rs types.Receipts) (*types.Header, []byte, []byte) {
			rs[2].Logs[0].Data = []byte{4}
			return h, encode(t, tx), encode(t, rs)
		}, ErrCommitment},
		{"transaction root", func(h *types.Header, tx types.Transactions, rs types.Receipts) (*types.Header, []byte, []byte) {
			h.TxHash = common.Hash{1}
			return h, encode(t, tx), encode(t, rs)
		}, ErrCommitment},
		{"receipt root", func(h *types.Header, tx types.Transactions, rs types.Receipts) (*types.Header, []byte, []byte) {
			h.ReceiptHash = common.Hash{1}
			return h, encode(t, tx), encode(t, rs)
		}, ErrCommitment},
		{"trailing data", func(h *types.Header, tx types.Transactions, rs types.Receipts) (*types.Header, []byte, []byte) {
			return h, append(encode(t, tx), 0xc0), encode(t, rs)
		}, ErrInput},
		{"oversize", func(h *types.Header, tx types.Transactions, rs types.Receipts) (*types.Header, []byte, []byte) {
			return h, make([]byte, MaxListBytes+1), encode(t, rs)
		}, ErrLimit},
		{"header extra", func(h *types.Header, tx types.Transactions, rs types.Receipts) (*types.Header, []byte, []byte) {
			h.Extra = make([]byte, MaxHeaderExtraBytes+1)
			return h, encode(t, tx), encode(t, rs)
		}, ErrLimit},
		{"nil number", func(h *types.Header, tx types.Transactions, rs types.Receipts) (*types.Header, []byte, []byte) {
			h.Number = nil
			return h, encode(t, tx), encode(t, rs)
		}, ErrInput},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			h, tx, rs := fixture(t)
			h, a, b := tt.change(h, tx, rs)
			result, err := CheckCompleteLists(h, a, b)
			if result != nil || !errors.Is(err, tt.want) {
				t.Fatalf("result=%+v error=%v want=%v", result, err, tt.want)
			}
		})
	}
}

func TestInvalidStatusAndReordering(t *testing.T) {
	h, txs, rs := fixture(t)
	bad := encode(t, []any{[]byte{2}, uint64(0), types.Bloom{}, []*types.Log{}})
	packet := encode(t, []rlp.RawValue{bad, encode(t, rs[1]), encode(t, rs[2])})
	if _, err := CheckCompleteLists(h, encode(t, txs), packet); !errors.Is(err, ErrInput) {
		t.Fatalf("status: %v", err)
	}
	// Same-type receipts ensure ordering fails at the root check, not type check.
	txs = types.Transactions{txs[0], types.NewTx(&types.LegacyTx{Nonce: 8, GasPrice: big.NewInt(0), Value: big.NewInt(0)})}
	rs = types.Receipts{rs[0], {Type: 0, Status: 0, CumulativeGasUsed: 1}}
	h.TxHash = types.DeriveSha(txs, trie.NewStackTrie(nil))
	h.ReceiptHash = types.DeriveSha(rs, trie.NewStackTrie(nil))
	rs[0], rs[1] = rs[1], rs[0]
	if _, err := CheckCompleteLists(h, encode(t, txs), encode(t, rs)); !errors.Is(err, ErrCommitment) {
		t.Fatalf("reorder: %v", err)
	}
}

func TestEmptyAndPostState(t *testing.T) {
	h := &types.Header{Number: big.NewInt(0), TxHash: types.EmptyRootHash, ReceiptHash: types.EmptyRootHash}
	if result, err := CheckCompleteLists(h, []byte{0xc0}, []byte{0xc0}); err != nil || len(result.Entries) != 0 {
		t.Fatalf("empty: %v", err)
	}
	h, txs, rs := fixture(t)
	rs[0].PostState = bytes.Repeat([]byte{1}, 32)
	h.ReceiptHash = types.DeriveSha(rs, trie.NewStackTrie(nil))
	result, err := CheckCompleteLists(h, encode(t, txs), encode(t, rs))
	if err != nil || result.Entries[0].Outcome != OutcomeUnknown {
		t.Fatalf("poststate: %v", err)
	}
}

func TestItemLimit(t *testing.T) {
	items := make([]rlp.RawValue, MaxItems+1)
	for i := range items {
		items[i] = rlp.RawValue{0xc0}
	}
	if _, err := splitList(encode(t, items)); !errors.Is(err, ErrLimit) {
		t.Fatalf("limit: %v", err)
	}
}

func FuzzCompleteLists(f *testing.F) {
	h, txs, rs := fixture(f)
	f.Add(encode(f, txs), encode(f, rs))
	f.Add([]byte{0xc0}, []byte{0xc0})
	f.Add([]byte{}, []byte{0xff})
	f.Fuzz(func(t *testing.T, tx, receipts []byte) {
		result, err := CheckCompleteLists(h, tx, receipts)
		if err != nil && result != nil {
			t.Fatal("partial success on error")
		}
		if result != nil && result.HeaderAuthentication != "not_checked" {
			t.Fatal("trust escalation")
		}
	})
}
