// SPDX-License-Identifier: GPL-3.0-or-later
package relay

import (
	"bytes"
	"errors"
	"math/big"
	"sync"
	"testing"
	"time"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/crypto"
	"github.com/ethereum/go-ethereum/p2p/enode"
)

// All load fixtures are zero-value transactions signed by a predictable,
// test-only key. No account, public connection, or actual payment is used.
func loadFixtures(tb testing.TB, count int) ([]*types.Transaction, [][]byte) {
	tb.Helper()
	key, err := crypto.ToECDSA(bytes.Repeat([]byte{2}, 32))
	if err != nil {
		tb.Fatal(err)
	}
	txs := make([]*types.Transaction, count)
	raws := make([][]byte, count)
	for i := range txs {
		tx := types.NewTransaction(uint64(i), common.Address{19: 1}, big.NewInt(0), 21000, big.NewInt(1), nil)
		txs[i], err = types.SignTx(tx, types.LatestSignerForChainID(big.NewInt(56)), key)
		if err != nil {
			tb.Fatal(err)
		}
		raws[i], err = txs[i].MarshalBinary()
		if err != nil {
			tb.Fatal(err)
		}
	}
	return txs, raws
}
func loadNode(tb testing.TB, count, peers, queue int) *Node {
	tb.Helper()
	chain, err := network("mainnet")
	if err != nil {
		tb.Fatal(err)
	}
	cfg := DefaultConfig()
	cfg.CacheCount = count
	cfg.CacheTTL = time.Hour
	n := &Node{cfg: cfg, chain: chain, cache: newStore(count, cfg.CacheBytes, cfg.CacheTTL), peers: make(map[enode.ID]*peerSession), stop: make(chan struct{})}
	for i := 0; i < peers; i++ {
		n.peers[enode.ID{byte(i + 1)}] = &peerSession{receiveTx: true, queue: make(chan outbound, queue), done: make(chan struct{})}
	}
	return n
}
func assertLoadBounds(t *testing.T, n *Node) {
	t.Helper()
	n.submitMu.Lock()
	defer n.submitMu.Unlock()
	n.mu.RLock()
	defer n.mu.RUnlock()
	n.cache.mu.Lock()
	defer n.cache.mu.Unlock()
	total := 0
	for _, e := range n.cache.entries {
		total += e.size
	}
	if len(n.cache.entries) > n.cfg.CacheCount || total != n.cache.bytes || total > n.cfg.CacheBytes {
		t.Fatalf("cache count=%d bytes=%d actual=%d", len(n.cache.entries), n.cache.bytes, total)
	}
	if err := cacheInvariantLocked(n.cache); err != nil {
		t.Fatal(err)
	}
	for _, p := range n.peers {
		if len(p.sent) > n.cfg.CacheCount || len(p.queue) > cap(p.queue) {
			t.Fatal("peer state exceeded configured bounds")
		}
	}
}
func drainLoad(t *testing.T, p *peerSession, seen map[common.Hash]bool) {
	t.Helper()
	for {
		select {
		case out := <-p.queue:
			txs, ok := out.data.(types.Transactions)
			if !ok {
				t.Fatalf("unexpected queued data %T", out.data)
			}
			for _, tx := range txs {
				if seen[tx.Hash()] {
					t.Fatalf("duplicate queued to same peer: %s", tx.Hash())
				}
				seen[tx.Hash()] = true
			}
		default:
			return
		}
	}
}

func TestLoadQueueRecoveryAndPeerReplacement(t *testing.T) {
	n := loadNode(t, 64, 6, 2)
	_, raws := loadFixtures(t, 65)
	seen := make(map[*peerSession]map[common.Hash]bool)
	for _, p := range n.peers {
		seen[p] = make(map[common.Hash]bool)
	}
	drainAll := func() {
		for p, hashes := range seen {
			drainLoad(t, p, hashes)
		}
	}
	for i := 0; i < 60; i++ {
		if _, err := n.Submit(raws[i]); err != nil {
			t.Fatal(err)
		}
		drainAll()
	}
	for i := 60; i < 62; i++ {
		if _, err := n.Submit(raws[i]); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := n.Submit(raws[62]); !errors.Is(err, ErrQueuesFull) {
		t.Fatalf("want full queues, got %v", err)
	}
	if count, _ := n.cache.stats(); count != 62 {
		t.Fatalf("failed admission retained entry: %d", count)
	}
	for i := 1; i <= 3; i++ {
		p := n.peers[enode.ID{byte(i)}]
		drainLoad(t, p, seen[p])
	}
	if got, err := n.Submit(raws[62]); err != nil || got.QueuedPeers != 3 {
		t.Fatalf("partial recovery %+v %v", got, err)
	}
	drainAll()
	if got, err := n.Submit(raws[62]); err != nil || got.QueuedPeers != 3 {
		t.Fatalf("remaining peers retry %+v %v", got, err)
	}
	drainAll()
	if _, err := n.Submit(raws[63]); err != nil {
		t.Fatal(err)
	}
	drainAll()
	if _, err := n.Submit(raws[64]); !errors.Is(err, ErrCacheFull) {
		t.Fatalf("want full cache, got %v", err)
	}
	id := enode.ID{1}
	old := n.peers[id]
	replacement := &peerSession{receiveTx: true, queue: make(chan outbound, 2), done: make(chan struct{})}
	n.mu.Lock()
	close(old.done)
	n.peers[id] = replacement
	n.mu.Unlock()
	seen[replacement] = make(map[common.Hash]bool)
	if got, err := n.Submit(raws[0]); err != nil || got.QueuedPeers != 1 {
		t.Fatalf("replacement retry %+v %v", got, err)
	}
	drainAll()
	if got, err := n.Submit(raws[0]); err != nil || got.QueuedPeers != 0 {
		t.Fatalf("duplicate retry %+v %v", got, err)
	}
	assertLoadBounds(t, n)
}

func TestLoadConcurrentSubmitHealthAndExpiry(t *testing.T) {
	n := loadNode(t, 64, 8, 64)
	txs, raws := loadFixtures(t, 64)
	// Expired fixtures prime both maps without wall-clock sleeping. Submit,
	// Health and prune race to clean them while new admissions occur.
	expired := time.Now().Add(-2 * time.Hour)
	for _, tx := range txs[:32] {
		if _, err := n.cache.add(tx, expired); err != nil {
			t.Fatal(err)
		}
		for _, p := range n.peers {
			if p.sent == nil {
				p.sent = make(map[common.Hash]time.Time)
			}
			p.sent[tx.Hash()] = expired.Add(time.Hour)
		}
	}
	start := make(chan struct{})
	var wg sync.WaitGroup
	for worker := 0; worker < 8; worker++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			for _, raw := range raws[32:] {
				if _, err := n.Submit(raw); err != nil {
					t.Errorf("concurrent Submit: %v", err)
				}
			}
		}()
	}
	wg.Add(2)
	go func() {
		defer wg.Done()
		<-start
		for i := 0; i < 256; i++ {
			h := n.Health()
			if h.CachedTransactions > 64 || h.CachedBytes > n.cfg.CacheBytes {
				t.Error("health exceeded cache bounds")
			}
		}
	}()
	go func() {
		defer wg.Done()
		<-start
		for i := 0; i < 256; i++ {
			n.prune(time.Now())
		}
	}()
	close(start)
	wg.Wait()
	assertLoadBounds(t, n)
	if count, _ := n.cache.stats(); count != 32 {
		t.Fatalf("cache count=%d, want 32", count)
	}
	for _, p := range n.peers {
		seen := make(map[common.Hash]bool)
		drainLoad(t, p, seen)
		if len(seen) != 32 || len(p.sent) != 32 {
			t.Fatalf("peer queued=%d sent=%d", len(seen), len(p.sent))
		}
	}
	// A second deterministic expiry epoch must free both cache and sent-map
	// capacity, and allow the other 32 hashes without stale-map starvation.
	n.submitMu.Lock()
	setCacheExpiryForTest(t, n.cache, expired)
	for _, p := range n.peers {
		for h := range p.sent {
			p.sent[h] = expired
		}
	}
	n.submitMu.Unlock()
	for _, raw := range raws[:32] {
		if _, err := n.Submit(raw); err != nil {
			t.Fatal(err)
		}
	}
	for _, p := range n.peers {
		seen := make(map[common.Hash]bool)
		drainLoad(t, p, seen)
		active := 0
		for _, deadline := range p.sent {
			if time.Now().Before(deadline) {
				active++
			}
		}
		if len(seen) != 32 || active != 32 || len(p.sent) > n.cfg.CacheCount {
			t.Fatalf("expiry recovery: queued=%d active=%d physical=%d", len(seen), active, len(p.sent))
		}
	}
	assertLoadBounds(t, n)
	// Expired metadata can remain until the next scheduled sweep. Force that
	// deadline without sleeping, then verify cleanup and active dedup together.
	n.submitMu.Lock()
	for _, p := range n.peers {
		p.sentNextSweep = expired
	}
	n.submitMu.Unlock()
	if got, err := n.Submit(raws[0]); err != nil || got.QueuedPeers != 0 || !got.Duplicate {
		t.Fatalf("sweep duplicate: %+v %v", got, err)
	}
	for _, p := range n.peers {
		if len(p.sent) != 32 || len(p.queue) != 0 {
			t.Fatal("scheduled sweep failed to reclaim only expired metadata")
		}
	}
	assertLoadBounds(t, n)
}

func TestLoadCacheByteLimitRecovery(t *testing.T) {
	n := loadNode(t, 64, 2, 32)
	txs, raws := loadFixtures(t, 32)
	limit := 0
	for _, tx := range txs[:30] {
		limit += int(tx.Size())
	}
	n.cfg.CacheBytes = limit
	n.cache.maxBytes = limit
	for _, raw := range raws[:30] {
		if _, err := n.Submit(raw); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := n.Submit(raws[30]); !errors.Is(err, ErrCacheFull) {
		t.Fatalf("byte limit: %v", err)
	}
	assertLoadBounds(t, n)
	n.cache.remove(txs[0].Hash())
	if _, err := n.Submit(raws[30]); err != nil {
		t.Fatal(err)
	}
	assertLoadBounds(t, n)
}

// These in-memory benchmarks exercise signature validation, cache admission,
// per-peer sent-map scans, and admission. They measure no network throughput.
func BenchmarkSubmitNearCapacity(b *testing.B) {
	txs, raws := loadFixtures(b, 901)
	for _, mode := range []string{"duplicate", "fresh_recycled"} {
		b.Run(mode, func(b *testing.B) {
			n := loadNode(b, 1024, 20, 1)
			now := time.Now()
			expiry := now.Add(time.Hour)
			for _, tx := range txs[:900] {
				if _, err := n.cache.add(tx, now); err != nil {
					b.Fatal(err)
				}
				for _, p := range n.peers {
					if p.sent == nil {
						p.sent = make(map[common.Hash]time.Time)
					}
					p.sent[tx.Hash()] = expiry
				}
			}
			raw := raws[899]
			if mode == "fresh_recycled" {
				raw = raws[900]
			}
			b.ReportAllocs()
			b.ResetTimer()
			for i := 0; i < b.N; i++ {
				got, err := n.Submit(raw)
				if err != nil {
					b.Fatal(err)
				}
				if mode == "fresh_recycled" {
					if got.QueuedPeers != 20 {
						b.Fatalf("queued=%d", got.QueuedPeers)
					}
					// Include bounded fixture recycling in this measurement rather than
					// hide per-iteration clock manipulation overhead.
					n.cache.remove(txs[900].Hash())
					for _, p := range n.peers {
						<-p.queue
						delete(p.sent, txs[900].Hash())
					}
				} else if got.QueuedPeers != 0 {
					b.Fatal("duplicate queued")
				}
			}
		})
	}
}

func TestLoadTargetExpiresBeforeSentSweep(t *testing.T) {
	n := loadNode(t, 8, 2, 8)
	txs, raws := loadFixtures(t, 2)
	for _, raw := range raws {
		if _, err := n.Submit(raw); err != nil {
			t.Fatal(err)
		}
	}
	for _, p := range n.peers {
		drainLoad(t, p, make(map[common.Hash]bool))
	}
	past := time.Now().Add(-time.Hour)
	nextSweep := time.Now().Add(time.Hour)
	n.submitMu.Lock()
	setCacheExpiryForTest(t, n.cache, past)
	for _, p := range n.peers {
		p.sentNextSweep = nextSweep
		for hash := range p.sent {
			p.sent[hash] = past
		}
	}
	n.submitMu.Unlock()
	got, err := n.Submit(raws[0])
	if err != nil || got.QueuedPeers != 2 || got.Duplicate {
		t.Fatalf("expired target retry: %+v %v", got, err)
	}
	cacheDeadline := n.cache.expiry(txs[0].Hash())
	for _, p := range n.peers {
		if !p.sentNextSweep.Equal(nextSweep) {
			t.Fatal("target retry unexpectedly required full sweep")
		}
		if deadline, ok := p.sent[txs[1].Hash()]; !ok || !deadline.Equal(past) {
			t.Fatal("unrelated expired metadata must remain until sweep in this fixture")
		}
		out := <-p.queue
		if !out.expires.Equal(cacheDeadline) || !time.Now().Before(out.expires) {
			t.Fatal("retried payload has stale or extended cache deadline")
		}
		queued, ok := out.data.(types.Transactions)
		if !ok || len(queued) != 1 || queued[0].Hash() != txs[0].Hash() {
			t.Fatal("incorrect retry payload")
		}
	}
	if got, err := n.Submit(raws[0]); err != nil || got.QueuedPeers != 0 || !got.Duplicate {
		t.Fatalf("active target dedup: %+v %v", got, err)
	}
	for _, p := range n.peers {
		if len(p.queue) != 0 {
			t.Fatal("duplicate active transaction queued")
		}
	}
	assertLoadBounds(t, n)
}

func TestLoadFullSentMapForcesReclamation(t *testing.T) {
	n := loadNode(t, 4, 2, 4)
	txs, raws := loadFixtures(t, 5)
	for _, raw := range raws[:4] {
		if _, err := n.Submit(raw); err != nil {
			t.Fatal(err)
		}
	}
	seen := make(map[*peerSession]map[common.Hash]bool)
	for _, p := range n.peers {
		seen[p] = make(map[common.Hash]bool)
		drainLoad(t, p, seen[p])
	}
	past := time.Now().Add(-time.Hour)
	n.submitMu.Lock()
	setCacheExpiryForTest(t, n.cache, past, txs[0].Hash())
	for _, p := range n.peers {
		p.sent[txs[0].Hash()] = past
		p.sentNextSweep = time.Now().Add(time.Hour)
	}
	n.submitMu.Unlock()
	if got, err := n.Submit(raws[4]); err != nil || got.QueuedPeers != 2 {
		t.Fatalf("full map reclamation: %+v %v", got, err)
	}
	for _, p := range n.peers {
		if _, ok := p.sent[txs[0].Hash()]; ok {
			t.Fatal("full map did not reclaim expired entry")
		}
		if len(p.sent) != 4 {
			t.Fatalf("sent map count=%d, want 4", len(p.sent))
		}
		drainLoad(t, p, seen[p])
		if len(seen[p]) != 5 {
			t.Fatal("new transaction not queued exactly once")
		}
	}
	if got, err := n.Submit(raws[1]); err != nil || got.QueuedPeers != 0 || !got.Duplicate {
		t.Fatalf("active entries lost during reclamation: %+v %v", got, err)
	}
	for _, p := range n.peers {
		if len(p.queue) != 0 {
			t.Fatal("reclamation caused duplicate queue admission")
		}
	}
	assertLoadBounds(t, n)
}

func TestLoadIdlePruneReclaimsExpiredMetadata(t *testing.T) {
	n := loadNode(t, 8, 2, 8)
	txs, raws := loadFixtures(t, 2)
	for _, raw := range raws {
		if _, err := n.Submit(raw); err != nil {
			t.Fatal(err)
		}
	}
	for _, p := range n.peers {
		drainLoad(t, p, make(map[common.Hash]bool))
	}
	past := time.Now().Add(-time.Hour)
	n.submitMu.Lock()
	setCacheExpiryForTest(t, n.cache, past, txs[0].Hash())
	for _, p := range n.peers {
		p.sent[txs[0].Hash()] = past
		p.sentNextSweep = time.Now().Add(time.Hour)
	}
	n.submitMu.Unlock()
	// Exercise the ticker's cleanup directly, with no new submissions to trigger
	// opportunistic sweeping and no wall-clock sleep.
	n.prune(time.Now())
	if count, _ := n.cache.stats(); count != 1 {
		t.Fatalf("idle cache count=%d, want 1", count)
	}
	for _, p := range n.peers {
		if len(p.sent) != 1 {
			t.Fatalf("idle sent map count=%d, want 1", len(p.sent))
		}
		if _, ok := p.sent[txs[1].Hash()]; !ok {
			t.Fatal("idle prune removed active hash")
		}
		if len(p.queue) != 0 {
			t.Fatal("idle prune queued payload")
		}
	}
	if got, err := n.Submit(raws[1]); err != nil || got.QueuedPeers != 0 || !got.Duplicate {
		t.Fatalf("idle prune weakened active dedup: %+v %v", got, err)
	}
	assertLoadBounds(t, n)
}

func TestLoadFullMapDuplicateDoesNotForceSweep(t *testing.T) {
	n := loadNode(t, 4, 2, 4)
	_, raws := loadFixtures(t, 4)
	for _, raw := range raws {
		if _, err := n.Submit(raw); err != nil {
			t.Fatal(err)
		}
	}
	deadline := time.Now().Add(time.Hour)
	for _, p := range n.peers {
		p.sentNextSweep = deadline
		drainLoad(t, p, make(map[common.Hash]bool))
	}
	got, err := n.Submit(raws[0])
	if err != nil || got.QueuedPeers != 0 || !got.Duplicate {
		t.Fatalf("full-map duplicate: %+v %v", got, err)
	}
	for _, p := range n.peers {
		if !p.sentNextSweep.Equal(deadline) {
			t.Fatal("duplicate forced an unnecessary capacity sweep")
		}
		if len(p.queue) != 0 {
			t.Fatal("duplicate was queued")
		}
	}
	assertLoadBounds(t, n)
}
func BenchmarkSubmitAtFullCapacity(b *testing.B) {
	n := loadNode(b, 1024, 20, 1)
	txs, raws := loadFixtures(b, 1024)
	now := time.Now()
	for _, tx := range txs {
		if _, err := n.cache.add(tx, now); err != nil {
			b.Fatal(err)
		}
		for _, p := range n.peers {
			if p.sent == nil {
				p.sent = make(map[common.Hash]time.Time)
			}
			p.sent[tx.Hash()] = now.Add(time.Hour)
			p.sentNextSweep = now.Add(time.Second)
		}
	}
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		result, err := n.Submit(raws[0])
		if err != nil || result.QueuedPeers != 0 || !result.Duplicate {
			b.Fatalf("unexpected duplicate result: %+v %v", result, err)
		}
	}
}
