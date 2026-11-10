// SPDX-License-Identifier: GPL-3.0-or-later
package evidence

import (
	"errors"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/trie"
	"math/big"
	"testing"
)

// Unsigned synthetic envelopes test commitments only, never transaction validity.
func TestAdditionalEnvelopeCommitments(t *testing.T) {
	for _, tx := range []*types.Transaction{types.NewTx(&types.BlobTx{}), types.NewTx(&types.SetCodeTx{})} {
		txs := types.Transactions{tx}
		rs := types.Receipts{{Type: tx.Type(), Status: 1}}
		h := &types.Header{Number: big.NewInt(1), TxHash: types.DeriveSha(txs, trie.NewStackTrie(nil)), ReceiptHash: types.DeriveSha(rs, trie.NewStackTrie(nil))}
		result, err := CheckCompleteLists(h, encode(t, txs), encode(t, rs))
		if err != nil || result.HeaderAuthentication != "not_checked" || result.Entries[0].TransactionType != tx.Type() {
			t.Fatalf("type%d: %+v %v", tx.Type(), result, err)
		}
	}
	txs := types.Transactions{types.NewTx(&types.BlobTx{Sidecar: &types.BlobTxSidecar{}})}
	rs := types.Receipts{{Type: types.BlobTxType, Status: 1}}
	h := &types.Header{Number: big.NewInt(1)}
	result, err := CheckCompleteLists(h, encode(t, txs), encode(t, rs))
	if result != nil || !errors.Is(err, ErrInput) {
		t.Fatalf("pool sidecar accepted: %+v %v", result, err)
	}
}
