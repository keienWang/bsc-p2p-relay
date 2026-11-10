// SPDX-License-Identifier: GPL-3.0-or-later
package relay

import (
	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/types"
	eth "github.com/ethereum/go-ethereum/eth/protocols/eth"
	"github.com/ethereum/go-ethereum/p2p"
	"github.com/ethereum/go-ethereum/rlp"
	"io"
	"testing"
	"time"
)

type captureRW struct{ codes chan uint64 }

func (rw *captureRW) ReadMsg() (p2p.Msg, error)  { return p2p.Msg{}, io.EOF }
func (rw *captureRW) WriteMsg(msg p2p.Msg) error { _ = msg.Discard(); rw.codes <- msg.Code; return nil }
func TestWriterDropsExpiredTransactions(t *testing.T) {
	n := bareNode(t)
	rw := &captureRW{codes: make(chan uint64, 2)}
	p := &peerSession{rw: rw, queue: make(chan outbound, 3), done: make(chan struct{})}
	defer close(p.done)
	p.queue <- outbound{code: eth.TransactionsMsg, data: types.Transactions{fixtureTx(t, 56, 0, false)}, expires: time.Now().Add(-time.Second)}
	p.queue <- outbound{code: eth.TransactionsMsg, data: types.Transactions{fixtureTx(t, 56, 1, false)}}
	p.queue <- outbound{code: eth.BlockHeadersMsg, data: []any{uint64(7), []uint64{}}}
	go n.writer(p)
	select {
	case code := <-rw.codes:
		if code != eth.BlockHeadersMsg {
			t.Fatalf("expired transaction emitted: %d", code)
		}
	case <-time.After(time.Second):
		t.Fatal("writer failed to drain control message")
	}
	select {
	case code := <-rw.codes:
		t.Fatalf("unexpected write: %d", code)
	default:
	}
}
func TestLargeDiscardedBlockIsNotDecoded(t *testing.T) {
	n := bareNode(t)
	msg := p2p.Msg{Code: eth.NewBlockMsg, Size: 3 * 1024 * 1024, Payload: nil}
	if err := n.handle(&peerSession{}, msg); err != nil {
		t.Fatal("legal large block must be discarded, not reject peer", err)
	}
	msg.Code = eth.GetBlockHeadersMsg
	if err := n.handle(&peerSession{}, msg); err == nil {
		t.Fatal("large request must be rejected before decoding")
	}
}

func TestPooledReplyRechecksCacheAtWrite(t *testing.T) {
	n := bareNode(t)
	tx := fixtureTx(t, 56, 0, false)
	if _, err := n.cache.add(tx, time.Now()); err != nil {
		t.Fatal(err)
	}
	out := outbound{code: eth.PooledTransactionsMsg, data: pooledReply{requestID: 99, hashes: []common.Hash{tx.Hash()}}}
	n.cache.remove(tx.Hash())
	raw, err := rlp.EncodeToBytes(n.outboundData(out))
	if err != nil {
		t.Fatal(err)
	}
	var got eth.PooledTransactionsPacket
	if err := rlp.DecodeBytes(raw, &got); err != nil {
		t.Fatal(err)
	}
	items, err := got.List.Items()
	if err != nil || len(items) != 0 || got.RequestId != 99 {
		t.Fatalf("expired pooled response: %d %d %v", got.RequestId, len(items), err)
	}
}
