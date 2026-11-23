// SPDX-License-Identifier: GPL-3.0-or-later
package relay

import (
	"bytes"
	"runtime"
	"testing"
	"time"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/types"
	eth "github.com/ethereum/go-ethereum/eth/protocols/eth"
	"github.com/ethereum/go-ethereum/p2p"
	"github.com/ethereum/go-ethereum/rlp"
)

func TestPooledReplyPreservesOrderExpiryAndFirstSizeOverflow(t *testing.T) {
	for _, tc := range []struct {
		name string
		size int
	}{
		{"exact_byte_limit", MaxRawTransaction},
		{"later_small_item_would_fit", MaxRawTransaction - 1024},
	} {
		t.Run(tc.name, func(t *testing.T) {
			n := protocolNode(t)
			t.Cleanup(n.Close)
			// These ordinary encoding fixtures stay in memory and are never sent.
			tx := func(nonce uint64, data int) *types.Transaction {
				return types.NewTx(&types.LegacyTx{Nonce: nonce, Data: make([]byte, data)})
			}
			// For these large payloads, the number of bytes in the RLP
			// length prefix stays fixed when adjusting the body length.
			probe := tx(1, tc.size-64)
			dataSize := tc.size - (int(probe.Size()) - (tc.size - 64))
			large, other := tx(1, dataSize), tx(2, dataSize)
			if large.Size() != uint64(tc.size) || other.Size() != uint64(tc.size) {
				t.Fatalf("large fixture sizes = %d/%d, want %d", large.Size(), other.Size(), tc.size)
			}
			expired, overflow, small := tx(3, 0), tx(4, MaxRawTransaction-64), tx(5, 0)
			if overflow.Size() <= 8192 || small.Size() >= 8192 {
				t.Fatal("fixtures must straddle the partial case's remaining byte allowance")
			}
			now := time.Now()
			for _, fixture := range []*types.Transaction{large, other, overflow, small, expired} {
				inserted := now
				if fixture == expired {
					// Insert at an earlier clock time *after* the live items.
					inserted = now.Add(-n.cfg.CacheTTL - time.Second)
				}
				if fresh, err := n.cache.add(fixture, inserted); !fresh || err != nil {
					t.Fatalf("cache fixture: fresh=%v err=%v", fresh, err)
				}
			}
			want := []common.Hash{large.Hash(), other.Hash(), large.Hash(), large.Hash(),
				other.Hash(), large.Hash(), other.Hash(), large.Hash()}
			hashes := append([]common.Hash{{31: 255}, expired.Hash()}, want...)
			hashes = append(hashes, overflow.Hash(), small.Hash())
			req := &eth.GetPooledTransactionsPacket{RequestId: 827, GetPooledTransactionsRequest: hashes}
			payload, err := rlp.EncodeToBytes(req)
			if err != nil {
				t.Fatal(err)
			}
			p := &peerSession{queue: make(chan outbound, 1), done: make(chan struct{})}
			if err := n.handle(p, p2p.Msg{Code: eth.GetPooledTransactionsMsg, Size: uint32(len(payload)), Payload: bytes.NewReader(payload)}); err != nil {
				t.Fatal(err)
			}
			out := <-p.queue
			payload, err = rlp.EncodeToBytes(n.outboundData(out))
			if err != nil {
				t.Fatal(err)
			}
			var got eth.PooledTransactionsPacket
			if err := rlp.DecodeBytes(payload, &got); err != nil {
				t.Fatal(err)
			}
			items, err := got.List.Items()
			if err != nil || len(items) != len(want) || got.RequestId != req.RequestId || out.code != eth.PooledTransactionsMsg {
				t.Fatalf("response code=%d id=%d count=%d err=%v", out.code, got.RequestId, len(items), err)
			}
			for i, item := range items {
				if item.Hash() != want[i] {
					t.Fatalf("response did not preserve request order and duplicates at index %d", i)
				}
			}
			if !n.cache.expiry(expired.Hash()).IsZero() {
				t.Fatal("lookup did not remove the requested expired entry")
			}
		})
	}
}

func BenchmarkPooledReplyAssembly(b *testing.B) {
	for _, tc := range []struct {
		name  string
		count int
		hits  int
	}{
		{"one_hit", 1, 1},
		{"mixed_256", 256, 128},
		{"all_miss_256", 256, 0},
	} {
		b.Run(tc.name, func(b *testing.B) {
			n := loadNode(b, 512, 0, 1)
			hashes := make([]common.Hash, tc.count)
			now := time.Now()
			for i := range hashes {
				tx := types.NewTx(&types.LegacyTx{Nonce: uint64(i)})
				hashes[i] = tx.Hash()
				if i < tc.hits {
					if fresh, err := n.cache.add(tx, now); !fresh || err != nil {
						b.Fatalf("cache fixture: fresh=%v err=%v", fresh, err)
					}
				}
			}
			out := outbound{code: eth.PooledTransactionsMsg, data: pooledReply{requestID: 1, hashes: hashes}}
			b.ReportAllocs()
			b.ResetTimer()
			for i := 0; i < b.N; i++ {
				runtime.KeepAlive(n.outboundData(out))
			}
		})
	}
}
