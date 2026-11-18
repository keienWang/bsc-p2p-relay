// SPDX-License-Identifier: GPL-3.0-or-later
//go:build loopbackrecovery

package relay

import (
	"encoding/json"
	"errors"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/ethereum/go-ethereum/core"
	"github.com/ethereum/go-ethereum/core/forkid"
	"github.com/ethereum/go-ethereum/crypto"
	eth "github.com/ethereum/go-ethereum/eth/protocols/eth"
	"github.com/ethereum/go-ethereum/p2p"
	"github.com/ethereum/go-ethereum/p2p/enode"
	"github.com/ethereum/go-ethereum/p2p/netutil"
	"github.com/ethereum/go-ethereum/params"
)

// Run with go test -tags loopbackrecovery ./internal/relay -run TestStaticPeerLoopbackRecovery.
// This no-transaction test uses actual TCP/RLPx, a synthetic chain, and an
// inbound-only handshake fixture. It exercises repeated static redial without
// shortening the upstream dial-history cooldown or replacing transports.
func TestStaticPeerLoopbackRecovery(t *testing.T) {
	cfg := DefaultConfig()
	cfg.ListenAddr = "127.0.0.1:0"
	cfg.NoDiscovery = true
	cfg.MaxPeers = 2
	n, err := New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(n.Close)

	chainCfg := *params.AllEthashProtocolChanges
	chainCfg.ChainID = big.NewInt(13371338)
	genesis := (&core.Genesis{Config: &chainCfg, GasLimit: 30_000_000, Difficulty: big.NewInt(1)}).ToBlock()
	n.chain = &chainView{config: &chainCfg, genesis: genesis}
	loopback, err := netutil.ParseNetlist("127.0.0.0/8")
	if err != nil {
		t.Fatal(err)
	}
	n.server.NetRestrict = loopback
	key, err := crypto.GenerateKey()
	if err != nil {
		t.Fatal(err)
	}
	status := eth.StatusPacket68{ProtocolVersion: 68, NetworkID: chainCfg.ChainID.Uint64(), TD: big.NewInt(1), Head: genesis.Hash(), Genesis: genesis.Hash(), ForkID: forkid.NewIDWithChain(n.chain)}
	ext, err := (&eth.UpgradeStatusExtension{}).Encode()
	if err != nil {
		t.Fatal(err)
	}
	remote := &p2p.Server{Config: p2p.Config{
		PrivateKey: key, Name: "relay-recovery-fixture", ListenAddr: "127.0.0.1:0",
		NoDiscovery: true, NoDial: true, NetRestrict: loopback, MaxPeers: 1,
		Protocols: []p2p.Protocol{{Name: "eth", Version: 68, Length: 17, Run: func(peer *p2p.Peer, rw p2p.MsgReadWriter) error {
			p := &peerSession{peer: peer, rw: rw}
			var gotStatus eth.StatusPacket68
			if err := exchange(p, eth.StatusMsg, &status, &gotStatus); err != nil {
				return err
			}
			if gotStatus.NetworkID != status.NetworkID || gotStatus.Genesis != status.Genesis {
				return errors.New("not the synthetic fixture network")
			}
			var gotUpgrade eth.UpgradeStatusPacket
			if err := exchange(p, eth.UpgradeStatusMsg, &eth.UpgradeStatusPacket{Extension: ext}, &gotUpgrade); err != nil {
				return err
			}
			msg, err := rw.ReadMsg()
			if err != nil {
				return err
			}
			_ = msg.Discard()
			return errors.New("unexpected application message in connection-only test")
		}}},
	}}
	if err := remote.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(remote.Stop)
	peer := remote.Self()
	if !peer.IP().Equal(net.IPv4(127, 0, 0, 1)) || peer.TCP() == 0 || !loopback.Contains(peer.IP()) {
		t.Fatalf("invalid loopback fixture address: %s", peer)
	}
	n.server.StaticNodes = []*enode.Node{peer}
	if err := n.Start(); err != nil {
		t.Fatal(err)
	}

	session := func() *peerSession {
		n.mu.RLock()
		defer n.mu.RUnlock()
		return n.peers[peer.ID()]
	}
	isReady := func() bool {
		h := n.Health()
		return h.ConnectedPeers == 1 && h.ReadyPeers == 1 && remote.PeerCount() == 1 && session() != nil
	}
	api := httptest.NewServer(n.ObservationHandler())
	t.Cleanup(api.Close)
	client := api.Client()
	client.Timeout = 2 * time.Second
	client.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	observe := func(label string, wantHTTPStatus, wantPeers int) Stats {
		t.Helper()
		var ready Health
		recoveryObservationGET(t, client, api.URL+"/readyz", wantHTTPStatus, &ready)
		if ready.ChainID != chainCfg.ChainID.String() || ready.ReadyPeers != wantPeers || ready.ConnectedPeers != wantPeers {
			t.Fatalf("%s: unexpected /readyz observation: %+v", label, ready)
		}
		var stats Stats
		recoveryObservationGET(t, client, api.URL+"/v1/stats", http.StatusOK, &stats)
		if stats.SchemaVersion != 1 || stats.InstanceID == "" || stats.CreatedAt.IsZero() || stats.ObservedAt.IsZero() {
			t.Fatalf("%s: missing /v1/stats metadata: %+v", label, stats)
		}
		if stats.Submit != (SubmitStats{}) || stats.HTTP != (HTTPStats{}) {
			t.Fatalf("%s: unexpected activity in observation-only stats: %+v", label, stats)
		}
		return stats
	}
	awaitRecoveryState(t, "initial static handshake", 10*time.Second, isReady)
	initialStats := observe("initial handshake", http.StatusOK, 1)
	sameInstance := func(label string, stats Stats) {
		t.Helper()
		if stats.InstanceID != initialStats.InstanceID || !stats.CreatedAt.Equal(initialStats.CreatedAt) {
			t.Fatalf("%s: stats node identity or creation time changed", label)
		}
	}
	for cycle := 1; cycle <= 2; cycle++ {
		previous := session()
		if previous == nil {
			t.Fatalf("cycle %d: ready session disappeared before deliberate disconnect", cycle)
		}
		started := time.Now()
		previous.peer.Disconnect(p2p.DiscNetworkError)
		awaitRecoveryState(t, "both ends disconnected", 10*time.Second, func() bool {
			h := n.Health()
			return h.ConnectedPeers == 0 && h.ReadyPeers == 0 && remote.PeerCount() == 0
		})
		sameInstance("disconnected", observe("disconnected", http.StatusServiceUnavailable, 0))
		// Pinned upstream holds a recent dial for 35 seconds.
		awaitRecoveryState(t, "fresh static session", 50*time.Second, func() bool {
			return isReady() && session() != previous
		})
		t.Logf("static cycle %d: recovered in %s", cycle, time.Since(started).Round(time.Millisecond))
		sameInstance("reconnected", observe("reconnected", http.StatusOK, 1))
	}
	if h := n.Health(); h.CachedTransactions != 0 || h.WrittenMessages != 0 || h.WriteFailures != 0 {
		t.Fatalf("unexpected application traffic or cached state: %+v", h)
	}
}

func recoveryObservationGET(t *testing.T, client *http.Client, url string, wantStatus int, dst any) {
	t.Helper()
	resp, err := client.Get(url)
	if err != nil {
		t.Fatalf("GET %s: %v", url, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != wantStatus {
		t.Fatalf("GET %s returned %d, want %d", url, resp.StatusCode, wantStatus)
	}
	if err := json.NewDecoder(resp.Body).Decode(dst); err != nil {
		t.Fatalf("decode GET %s: %v", url, err)
	}
}

func awaitRecoveryState(t *testing.T, label string, timeout time.Duration, condition func() bool) {
	t.Helper()
	deadline := time.NewTimer(timeout)
	defer deadline.Stop()
	ticks := time.NewTicker(10 * time.Millisecond)
	defer ticks.Stop()
	for !condition() {
		select {
		case <-ticks.C:
		case <-deadline.C:
			t.Fatalf("timed out after %s waiting for %s", timeout, label)
		}
	}
}
