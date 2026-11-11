// SPDX-License-Identifier: GPL-3.0-or-later
package relay

import (
	"errors"
	"net"
	"net/netip"
	"sync"
	"testing"
	"time"

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
