// SPDX-License-Identifier: GPL-3.0-or-later

// Package evidence checks complete transaction and eth/68 receipt lists against
// caller-supplied header commitments. It does not authenticate headers, execute
// transactions, choose a canonical chain, or establish confirmations or finality.
package evidence

import (
	"errors"
	"fmt"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/rlp"
	"github.com/ethereum/go-ethereum/trie"
)

// Limits are local resource policies, not claims about maximum valid BSC blocks.
// A larger otherwise-valid block is unsupported, not evidence of chain invalidity.
const (
	MaxListBytes        = 8 << 20
	MaxItems            = 32768
	MaxHeaderExtraBytes = 64 << 10
)

var (
	ErrLimit      = errors.New("evidence resource limit exceeded")
	ErrInput      = errors.New("invalid evidence input")
	ErrCommitment = errors.New("supplied header commitment mismatch")
)

type Outcome string

const (
	OutcomeSuccess Outcome = "receipt_status_success"
	OutcomeFailure Outcome = "receipt_status_failure"
	OutcomeUnknown Outcome = "post_state_only"
)

// Entry describes consensus-encoded receipt data at a transaction list index.
// Status is only an outcome claim committed by the supplied header, not an
// independently executed result. RPC-only receipt metadata is never trusted.
type Entry struct {
	Index             uint64
	TransactionHash   common.Hash
	TransactionType   uint8
	Outcome           Outcome
	CumulativeGasUsed uint64
}

// ConditionalResult is deliberately not a VerifiedHeader or confirmation token.
// All entries remain conditional on independent authentication of HeaderHash.
type ConditionalResult struct {
	HeaderHash           common.Hash
	HeaderAuthentication string
	Entries              []Entry
}

// CheckCompleteLists takes two RLP lists, not complete eth response packets:
// the transaction list from one block body and that block's complete eth/68
// receipt list. Empty blocks require the explicit RLP empty list, not nil bytes.
// The caller must not concurrently mutate header or input bytes during this call.
// Byte limits apply before decoding; each list is preflighted for item count.
func CheckCompleteLists(header *types.Header, transactionsRLP, receiptsRLP []byte) (*ConditionalResult, error) {
	if header == nil || header.Number == nil || header.Number.Sign() < 0 || !header.Number.IsUint64() {
		return nil, fmt.Errorf("%w: missing or invalid header number", ErrInput)
	}
	if len(header.Extra) > MaxHeaderExtraBytes {
		return nil, ErrLimit
	}
	if (header.Difficulty != nil && header.Difficulty.Sign() < 0) || (header.BaseFee != nil && header.BaseFee.Sign() < 0) {
		return nil, fmt.Errorf("%w: negative header integer", ErrInput)
	}
	if err := header.SanityCheck(); err != nil {
		return nil, fmt.Errorf("%w: header: %v", ErrInput, err)
	}
	txItems, err := splitList(transactionsRLP)
	if err != nil {
		return nil, fmt.Errorf("transactions: %w", err)
	}
	receiptItems, err := splitList(receiptsRLP)
	if err != nil {
		return nil, fmt.Errorf("receipts: %w", err)
	}
	if len(txItems) != len(receiptItems) {
		return nil, fmt.Errorf("%w: transaction/receipt count mismatch", ErrInput)
	}
	txs := make(types.Transactions, len(txItems))
	receipts := make(types.Receipts, len(receiptItems))
	entries := make([]Entry, len(txItems))
	for i := range txItems {
		tx := new(types.Transaction)
		if err := rlp.DecodeBytes(txItems[i], tx); err != nil {
			return nil, fmt.Errorf("%w: transaction %d: %v", ErrInput, i, err)
		}
		if tx.BlobTxSidecar() != nil {
			return nil, fmt.Errorf("%w: block transaction %d contains a pool sidecar", ErrInput, i)
		}
		r := new(types.Receipt)
		if err := rlp.DecodeBytes(receiptItems[i], r); err != nil {
			return nil, fmt.Errorf("%w: receipt %d: %v", ErrInput, i, err)
		}
		if tx.Type() != r.Type {
			return nil, fmt.Errorf("%w: receipt %d type mismatch", ErrInput, i)
		}
		// DecodeRLP rejects invalid status encodings, unlike encoding an arbitrary
		// in-memory Receipt, which normalizes every nonzero Status to success.
		outcome := OutcomeUnknown
		if len(r.PostState) == 0 {
			if r.Status == types.ReceiptStatusSuccessful {
				outcome = OutcomeSuccess
			} else {
				outcome = OutcomeFailure
			}
		}
		txs[i], receipts[i] = tx, r
		entries[i] = Entry{Index: uint64(i), TransactionHash: tx.Hash(), TransactionType: tx.Type(), Outcome: outcome, CumulativeGasUsed: r.CumulativeGasUsed}
	}
	if types.DeriveSha(txs, trie.NewStackTrie(nil)) != header.TxHash {
		return nil, fmt.Errorf("%w: transactions", ErrCommitment)
	}
	if types.DeriveSha(receipts, trie.NewStackTrie(nil)) != header.ReceiptHash {
		return nil, fmt.Errorf("%w: receipts", ErrCommitment)
	}
	return &ConditionalResult{HeaderHash: header.Hash(), HeaderAuthentication: "not_checked", Entries: entries}, nil
}

func splitList(input []byte) ([]rlp.RawValue, error) {
	if len(input) > MaxListBytes {
		return nil, ErrLimit
	}
	content, rest, err := rlp.SplitList(input)
	if err != nil || len(rest) != 0 {
		return nil, fmt.Errorf("%w: expected exactly one RLP list", ErrInput)
	}
	var items []rlp.RawValue
	for len(content) != 0 {
		if len(items) == MaxItems {
			return nil, ErrLimit
		}
		_, _, tail, err := rlp.Split(content)
		if err != nil {
			return nil, fmt.Errorf("%w: malformed list item: %v", ErrInput, err)
		}
		items = append(items, content[:len(content)-len(tail)])
		content = tail
	}
	return items, nil
}
