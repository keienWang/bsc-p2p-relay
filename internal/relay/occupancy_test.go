// SPDX-License-Identifier: GPL-3.0-or-later
package relay

import (
	"fmt"
	"github.com/ethereum/go-ethereum/common"
	"testing"
	"time"
)

func BenchmarkDuplicateOccupancy(b *testing.B) {
	txs, raws := loadFixtures(b, 1024)
	for _, count := range []int{900, 1024} {
		b.Run(fmt.Sprintf("count_%d", count), func(b *testing.B) {
			n := loadNode(b, 1024, 20, 1)
			now := time.Now()
			expiry := now.Add(time.Hour)
			for _, p := range n.peers {
				p.sent = make(map[common.Hash]time.Time)
				p.sentNextSweep = expiry
			}
			for _, tx := range txs[:count] {
				if _, err := n.cache.add(tx, now); err != nil {
					b.Fatal(err)
				}
				for _, p := range n.peers {
					p.sent[tx.Hash()] = expiry
				}
			}
			b.ReportAllocs()
			b.ResetTimer()
			for i := 0; i < b.N; i++ {
				got, err := n.Submit(raws[count-1])
				if err != nil || got.QueuedPeers != 0 {
					b.Fatalf("%+v %v", got, err)
				}
			}
		})
	}
}
