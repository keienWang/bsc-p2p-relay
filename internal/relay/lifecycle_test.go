// SPDX-License-Identifier: GPL-3.0-or-later
package relay

import (
	"errors"
	"net"
	"net/netip"
	"sync"
	"testing"
	"time"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/p2p/enode"
	"github.com/syndtr/goleveldb/leveldb"
)

func assertNodeClosed(t *testing.T, n *Node) {
	t.Helper()
	select {
	case <-n.stop:
	default:
		t.Fatal("closed node has an open stop channel")
	}
	if n.maintenanceDone != nil {
		select {
		case <-n.maintenanceDone:
		default:
			t.Fatal("Close returned before local maintenance stopped")
		}
	}
	if err := n.Start(); !errors.Is(err, ErrNodeClosed) {
		t.Fatalf("Start after terminal shutdown = %v, want ErrNodeClosed", err)
	}
	n.Close()
}

func assertListenerReleased(t *testing.T, address string) {
	t.Helper()
	listener, err := net.Listen("tcp", address)
	if err != nil {
		t.Fatalf("loopback listener still held after Close: %v", err)
	}
	if err := listener.Close(); err != nil {
		t.Fatal(err)
	}
}

// Closing a Node is terminal even if it has never been started. Otherwise the
// consumed shutdown guard leaves a later server running with no live writers.
func TestNodeCloseBeforeStartIsTerminal(t *testing.T) {
	n := protocolNode(t)
	// This direct cleanup also makes the regression safe on the broken code:
	// the public Close method can no longer stop a server started after Close.
	t.Cleanup(n.server.Stop)
	n.Close()
	if err := n.Start(); err == nil {
		t.Fatal("Start succeeded after Close; later Close cannot stop the server")
	}
	assertNodeClosed(t, n)
	if n.maintenanceDone != nil {
		t.Fatal("never-started node created a maintenance worker")
	}
}

func TestNodeCloseAfterStartIsTerminal(t *testing.T) {
	n := protocolNode(t)
	t.Cleanup(n.Close)
	if err := n.Start(); err != nil {
		t.Fatal(err)
	}
	address := n.server.ListenAddr
	n.Close()
	assertNodeClosed(t, n)
	assertListenerReleased(t, address)
}

func TestNodeCloseReleasesLocalTransactionCache(t *testing.T) {
	for _, mode := range []string{"before_start", "after_start"} {
		t.Run(mode, func(t *testing.T) {
			n := protocolNode(t)
			t.Cleanup(n.Close)
			if mode == "after_start" {
				n.server.NoDial = true
				if err := n.Start(); err != nil {
					t.Fatal(err)
				}
			}
			// Submit only to an in-process synthetic queue with no writer.
			n.mu.Lock()
			p := addFakePeer(n, true, 1)
			n.mu.Unlock()
			tx := fixtureTx(t, 56, 1, false)
			raw := rawFixture(t, tx)
			result, err := n.Submit(raw)
			if err != nil || result.QueuedPeers != 1 {
				t.Fatalf("local fixture admission = %+v, %v", result, err)
			}
			<-p.queue
			before := n.Health()
			if before.CachedTransactions != 1 || before.CachedBytes != int(tx.Size()) {
				t.Fatalf("fixture cache = %+v", before)
			}
			identity := n.Stats().InstanceID

			n.Close()
			n.Close()
			after := n.Health()
			if after.CachedTransactions != 0 || after.CachedBytes != 0 {
				t.Fatalf("terminal Node still reports cached contents: %+v", after)
			}
			n.cache.mu.Lock()
			retainedBacking := n.cache.entries != nil || n.cache.expiries != nil || !n.cache.expiryUpperBound.IsZero()
			n.cache.mu.Unlock()
			if retainedBacking {
				t.Fatal("terminal cache kept the map, heap backing or expiry bound")
			}
			stats := n.Stats()
			if stats.InstanceID != identity || stats.Submit.AcceptedNewCacheLifetimeTotal != 1 {
				t.Fatalf("closing cleared observation history: %+v", stats)
			}
			if _, err := n.Submit(raw); !errors.Is(err, ErrNodeClosed) {
				t.Fatalf("Submit after terminal cache release = %v, want ErrNodeClosed", err)
			}
			if len(p.queue) != 0 || n.Health().CachedTransactions != 0 {
				t.Fatal("submission after Close re-populated local state")
			}
		})
	}
}

func TestDelayedMaintenanceWakeUsesCurrentTime(t *testing.T) {
	n := bareNode(t)
	n.cache = newStore(4, MaxRawTransaction*4, 4*time.Hour)
	now := time.Now()
	expired, live := fixtureTx(t, 56, 1, false), fixtureTx(t, 56, 2, false)
	for _, fixture := range []struct {
		tx  *types.Transaction
		age time.Duration
	}{
		{expired, 5 * time.Hour}, // deadline one hour before handling the wake
		{live, 3 * time.Hour},    // deadline one hour after handling it
	} {
		if fresh, err := n.cache.add(fixture.tx, now.Add(-fixture.age)); err != nil || !fresh {
			t.Fatalf("fixture cache insertion = %v, %v", fresh, err)
		}
	}
	p := &peerSession{}
	p.recordSent(expired.Hash(), now.Add(-time.Hour))
	p.recordSent(live.Hash(), now.Add(time.Hour))
	n.mu.Lock()
	n.setPeerLocked(enode.ID{1}, p)
	n.mu.Unlock()

	ticks := make(chan time.Time)
	done := make(chan struct{})
	go func() {
		defer close(done)
		n.maintenanceLoop(ticks)
	}()
	select {
	case ticks <- now.Add(-2 * time.Hour): // an old tick predating even the expired deadline
	case <-time.After(5 * time.Second):
		close(n.stop)
		t.Fatal("maintenance worker did not receive its wake")
	}
	close(n.stop)
	select {
	case <-done: // The only selected wake has finished pruning.
	case <-time.After(5 * time.Second):
		t.Fatal("maintenance did not stop")
	}

	// Observe physical storage; Health would independently prune the cache.
	n.cache.mu.Lock()
	expiredEntry, liveEntry := n.cache.entries[expired.Hash()], n.cache.entries[live.Hash()]
	n.cache.mu.Unlock()
	if expiredEntry != nil || liveEntry == nil {
		t.Errorf("cache expired present=%v live present=%v, want false/true", expiredEntry != nil, liveEntry != nil)
	}
	n.submitMu.Lock()
	_, expiredSent := p.sent[expired.Hash()]
	_, liveSent := p.sent[live.Hash()]
	n.submitMu.Unlock()
	if expiredSent || !liveSent {
		t.Errorf("sent expired present=%v live present=%v, want false/true", expiredSent, liveSent)
	}
}

func TestNodeIdleMaintenanceRunsAcrossSuccessiveTicks(t *testing.T) {
	n := protocolNode(t)
	t.Cleanup(n.Close)
	n.server.NoDial = true // There are no peers to dial; also enforce it in the fixture.
	p := &peerSession{}
	first, second, live := common.Hash{1}, common.Hash{2}, common.Hash{3}
	due, future := time.Now().Add(-time.Second), time.Now().Add(time.Hour)
	p.recordSent(first, due)
	p.recordSent(second, future)
	p.recordSent(live, future)
	// Inject bookkeeping only, never a connection, transport, or transaction.
	n.setPeerLocked(enode.ID{1}, p)
	if err := n.Start(); err != nil {
		t.Fatal(err)
	}
	address := n.server.ListenAddr

	awaitPrune := func(expired, retained common.Hash, wantCount int) {
		t.Helper()
		timer := time.NewTimer(5 * time.Second)
		defer timer.Stop()
		poll := time.NewTicker(20 * time.Millisecond)
		defer poll.Stop()
		for {
			// Do not call Health or prune: only the periodic worker may clean up.
			n.submitMu.Lock()
			_, present := p.sent[expired]
			kept := p.sent[retained]
			count := len(p.sent)
			n.submitMu.Unlock()
			if !present {
				if !kept.Equal(future) || count != wantCount {
					t.Fatalf("idle pass retained %d records (want %d) and live deadline %v (want %v)",
						count, wantCount, kept, future)
				}
				return
			}
			select {
			case <-poll.C:
			case <-timer.C:
				t.Fatalf("idle worker did not reclaim hash %x", expired[0])
			}
		}
	}
	awaitPrune(first, second, 2)
	// Make a second deadline due only after observing the first cleanup. A
	// startup-only or one-shot maintenance call cannot pass the second phase.
	n.submitMu.Lock()
	p.recordSent(second, due)
	n.submitMu.Unlock()
	awaitPrune(second, live, 1)
	n.Close()
	assertNodeClosed(t, n)
	assertListenerReleased(t, address)
}

func TestNodeDuplicateStartKeepsServerRunning(t *testing.T) {
	n := protocolNode(t)
	t.Cleanup(n.Close)
	if err := n.Start(); err != nil {
		t.Fatal(err)
	}
	maintenance := n.maintenanceDone
	if err := n.Start(); err == nil || errors.Is(err, ErrNodeClosed) {
		t.Fatalf("duplicate Start = %v, want nonterminal already-running error", err)
	}
	select {
	case <-n.stop:
		t.Fatal("duplicate Start stopped the existing node")
	default:
	}
	if n.maintenanceDone != maintenance {
		t.Fatal("duplicate Start replaced the maintenance worker")
	}
	listener, err := net.Listen("tcp", n.server.ListenAddr)
	if err == nil {
		listener.Close()
		t.Fatal("duplicate Start released the existing listener")
	}
	n.Close()
	assertNodeClosed(t, n)
}

func TestNodeFailedStartIsTerminal(t *testing.T) {
	occupied, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer occupied.Close()
	n := protocolNode(t)
	n.server.ListenAddr = occupied.Addr().String()
	t.Cleanup(n.Close)
	if err := n.Start(); err == nil {
		t.Fatal("Start succeeded on an occupied loopback port")
	}
	assertNodeClosed(t, n)
	if n.maintenanceDone != nil {
		t.Fatal("failed Start created a maintenance worker")
	}
	local := n.server.LocalNode()
	if local == nil {
		t.Fatal("fixture did not reach the database-before-listen failure stage")
	}
	if err := local.Database().UpdateLastPingReceived(enode.ID{}, netip.MustParseAddr("127.0.0.1"), time.Now()); !errors.Is(err, leveldb.ErrClosed) {
		t.Fatalf("failed Start left its temporary database open: %v", err)
	}
	// Releasing the port does not make the same instance reusable. A new Node
	// is the supported retry path, with a fresh upstream server and stop signal.
	if err := occupied.Close(); err != nil {
		t.Fatal(err)
	}
	if err := n.Start(); !errors.Is(err, ErrNodeClosed) {
		t.Fatalf("retry after failed Start = %v, want ErrNodeClosed", err)
	}
	replacement := protocolNode(t)
	replacement.server.ListenAddr = n.server.ListenAddr
	t.Cleanup(replacement.Close)
	if err := replacement.Start(); err != nil {
		t.Fatalf("fresh Node could not use released listener: %v", err)
	}
}

func TestNodeConcurrentStartAndClose(t *testing.T) {
	for range 16 {
		n := protocolNode(t)
		// Keep a safety cleanup even if an assertion fails in this iteration.
		t.Cleanup(n.Close)
		var workers sync.WaitGroup
		start := make(chan struct{})
		results := make(chan error, 1)
		workers.Add(5)
		go func() {
			defer workers.Done()
			<-start
			results <- n.Start()
		}()
		for range 4 {
			go func() {
				defer workers.Done()
				<-start
				n.Close()
			}()
		}
		close(start)
		workers.Wait()
		if err := <-results; err != nil && !errors.Is(err, ErrNodeClosed) {
			t.Fatalf("concurrent Start = %v", err)
		}
		assertNodeClosed(t, n)
		assertListenerReleased(t, n.server.ListenAddr)
	}
}
