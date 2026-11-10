// SPDX-License-Identifier: GPL-3.0-or-later
package relay

import (
	"bytes"
	"encoding/hex"
	"errors"
	"math/big"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/crypto"
	"github.com/ethereum/go-ethereum/p2p/enode"
)

func fixtureTx(t *testing.T, chain int64, nonce uint64, typed bool) *types.Transaction {
	t.Helper()
	key, err := crypto.GenerateKey()
	if err != nil {
		t.Fatal(err)
	}
	to := common.HexToAddress("0x0000000000000000000000000000000000000001")
	var tx *types.Transaction
	if typed {
		tx = types.NewTx(&types.DynamicFeeTx{ChainID: big.NewInt(chain), Nonce: nonce, GasTipCap: big.NewInt(1), GasFeeCap: big.NewInt(2), Gas: 21000, To: &to, Value: big.NewInt(0)})
	} else {
		tx = types.NewTx(&types.LegacyTx{Nonce: nonce, GasPrice: big.NewInt(1), Gas: 21000, To: &to, Value: big.NewInt(0)})
	}
	signed, err := types.SignTx(tx, types.LatestSignerForChainID(big.NewInt(chain)), key)
	if err != nil {
		t.Fatal(err)
	}
	return signed
}
func rawFixture(t *testing.T, tx *types.Transaction) []byte {
	t.Helper()
	raw, err := tx.MarshalBinary()
	if err != nil {
		t.Fatal(err)
	}
	return raw
}
func bareNode(t *testing.T) *Node {
	t.Helper()
	chain, err := network("mainnet")
	if err != nil {
		t.Fatal(err)
	}
	cfg := DefaultConfig()
	return &Node{cfg: cfg, chain: chain, cache: newStore(4, 1024*1024, time.Minute), peers: make(map[enode.ID]*peerSession), stop: make(chan struct{})}
}
func addFakePeer(n *Node, receive bool, capacity int) *peerSession {
	p := &peerSession{receiveTx: receive, queue: make(chan outbound, capacity), done: make(chan struct{})}
	n.peers[enode.ID{1}] = p
	return p
}

func TestValidateSignedTransactions(t *testing.T) {
	for _, typed := range []bool{false, true} {
		tx := fixtureTx(t, 56, 1, typed)
		got, sender, err := validate(rawFixture(t, tx), big.NewInt(56))
		if err != nil || got.Hash() != tx.Hash() || sender == (common.Address{}) {
			t.Fatalf("validation: %v", err)
		}
	}
}
func TestValidateRejectsWrongChainAndMalformed(t *testing.T) {
	for _, raw := range [][]byte{nil, {1, 2, 3}, bytes.Repeat([]byte{0}, MaxRawTransaction+1), rawFixture(t, fixtureTx(t, 97, 1, false))} {
		if _, _, err := validate(raw, big.NewInt(56)); err == nil {
			t.Fatal("accepted invalid transaction")
		}
	}
}
func TestValidateRejectsUnsignedAndUnprotected(t *testing.T) {
	tx := types.NewTransaction(0, common.Address{}, big.NewInt(0), 21000, big.NewInt(1), nil)
	if _, _, err := validate(rawFixture(t, tx), big.NewInt(56)); err == nil {
		t.Fatal("accepted unsigned")
	}
	key, _ := crypto.GenerateKey()
	signed, _ := types.SignTx(tx, types.HomesteadSigner{}, key)
	if _, _, err := validate(rawFixture(t, signed), big.NewInt(56)); err == nil {
		t.Fatal("accepted unprotected")
	}
}
func TestStoreLimitsExpiryAndDedup(t *testing.T) {
	now := time.Now()
	a, b := fixtureTx(t, 56, 1, false), fixtureTx(t, 56, 2, false)
	s := newStore(1, MaxRawTransaction, time.Second)
	if fresh, err := s.add(a, now); !fresh || err != nil {
		t.Fatal(err)
	}
	if fresh, err := s.add(a, now); fresh || err != nil {
		t.Fatal("not idempotent")
	}
	if _, err := s.add(b, now); !errors.Is(err, ErrCacheFull) {
		t.Fatal("count limit")
	}
	if s.get(a.Hash(), now.Add(time.Second)) != nil {
		t.Fatal("expired retained")
	}
	if _, err := s.add(b, now.Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	s = newStore(10, 1, time.Second)
	if _, err := s.add(a, now); !errors.Is(err, ErrCacheFull) {
		t.Fatal("byte limit")
	}
}
func TestSubmitAdmissionAndQueueBounds(t *testing.T) {
	n := bareNode(t)
	raw := rawFixture(t, fixtureTx(t, 56, 0, true))
	if _, err := n.Submit(raw); !errors.Is(err, ErrNoPeers) {
		t.Fatal(err)
	}
	p := addFakePeer(n, true, 1)
	got, err := n.Submit(raw)
	if err != nil || got.QueuedPeers != 1 || got.Stage != "queued" {
		t.Fatalf("%+v %v", got, err)
	}
	again, err := n.Submit(raw)
	if err != nil || !again.Duplicate || again.QueuedPeers != 0 {
		t.Fatalf("dedup: %+v %v", again, err)
	}
	other := rawFixture(t, fixtureTx(t, 56, 1, false))
	if _, err = n.Submit(other); !errors.Is(err, ErrQueuesFull) {
		t.Fatal(err)
	}
	<-p.queue
	if retried, err := n.Submit(other); err != nil || retried.Duplicate || retried.QueuedPeers != 1 {
		t.Fatalf("retry: %+v %v", retried, err)
	}
}
func TestSubmitRespectsDisableBroadcast(t *testing.T) {
	n := bareNode(t)
	addFakePeer(n, false, 10)
	if _, err := n.Submit(rawFixture(t, fixtureTx(t, 56, 0, false))); !errors.Is(err, ErrNoPeers) {
		t.Fatal(err)
	}
}
func TestConcurrentDuplicateSubmission(t *testing.T) {
	n := bareNode(t)
	p := addFakePeer(n, true, 64)
	raw := rawFixture(t, fixtureTx(t, 56, 0, false))
	var wg sync.WaitGroup
	for i := 0; i < 32; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if _, err := n.Submit(raw); err != nil {
				t.Error(err)
			}
		}()
	}
	wg.Wait()
	if len(p.queue) != 1 {
		t.Fatal("duplicate queued more than once")
	}
}
func TestLoopbackOnly(t *testing.T) {
	for _, address := range []string{"127.0.0.1:8787", "[::1]:8787"} {
		if err := LoopbackAddress(address); err != nil {
			t.Fatal(err)
		}
	}
	for _, address := range []string{":8787", "0.0.0.0:8787", "localhost:8787", "192.168.0.1:8787"} {
		if LoopbackAddress(address) == nil {
			t.Fatalf("unsafe bind %s", address)
		}
	}
}
func TestHTTPReadinessAndGuards(t *testing.T) {
	n := bareNode(t)
	h := n.Handler()
	for _, tc := range []struct {
		method, path, body, host, origin, content string
		status                                    int
	}{
		{"GET", "/healthz", "", "localhost", "", "", 200},
		{"GET", "/readyz", "", "127.0.0.1:8787", "", "", 503},
		{"GET", "/healthz", "", "attacker.example", "", "", 403},
		{"POST", "/v1/transactions", "{}", "localhost", "https://attacker.example", "application/json", 403},
		{"POST", "/v1/transactions", "{}", "localhost", "", "text/plain", 415},
		{"POST", "/v1/transactions", "{\"secret\":\"no\"}", "localhost", "", "application/json", 400},
		{"POST", "/v1/transactions", "{} {}", "localhost", "", "application/json", 400},
		{"POST", "/v1/transactions", "{\"raw_tx\":\"0xqq\"}", "localhost", "", "application/json", 400},
		{"DELETE", "/v1/transactions", "", "localhost", "", "", 405},
	} {
		r := httptest.NewRequest(tc.method, tc.path, strings.NewReader(tc.body))
		r.Host = tc.host
		r.Header.Set("Origin", tc.origin)
		r.Header.Set("Content-Type", tc.content)
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		if w.Code != tc.status {
			t.Fatalf("%s %s: %d %s", tc.method, tc.path, w.Code, w.Body.String())
		}
	}
}
func TestHTTPValidSignedTransaction(t *testing.T) {
	n := bareNode(t)
	addFakePeer(n, true, 64)
	raw := rawFixture(t, fixtureTx(t, 56, 0, false))
	r := httptest.NewRequest(http.MethodPost, "http://localhost/v1/transactions", strings.NewReader(`{"raw_tx":"0x`+hex.EncodeToString(raw)+`"}`))
	r.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	n.Handler().ServeHTTP(w, r)
	if w.Code != 202 || !strings.Contains(w.Body.String(), `"queued_peers":1`) {
		t.Fatal(w.Code, w.Body.String())
	}
}

func TestCachedSubmissionRetriesNewPeer(t *testing.T) {
	n := bareNode(t)
	old := addFakePeer(n, true, 1)
	raw := rawFixture(t, fixtureTx(t, 56, 0, false))
	if _, err := n.Submit(raw); err != nil {
		t.Fatal(err)
	}
	close(old.done)
	replacement := addFakePeer(n, true, 1)
	got, err := n.Submit(raw)
	if err != nil || !got.Duplicate || got.QueuedPeers != 1 || len(replacement.queue) != 1 {
		t.Fatalf("retry to new peer: %+v %v", got, err)
	}
}

func TestExpiredCacheDoesNotSuppressRequeue(t *testing.T) {
	n := bareNode(t)
	p := addFakePeer(n, true, 2)
	tx := fixtureTx(t, 56, 0, false)
	raw := rawFixture(t, tx)
	if _, err := n.Submit(raw); err != nil {
		t.Fatal(err)
	}
	// Advance both synchronized expiration records without a wall-clock sleep.
	n.cache.mu.Lock()
	e := n.cache.entries[tx.Hash()]
	e.expires = time.Now().Add(-time.Second)
	n.cache.entries[tx.Hash()] = e
	n.cache.mu.Unlock()
	p.sent[tx.Hash()] = e.expires
	got, err := n.Submit(raw)
	if err != nil || got.Duplicate || got.QueuedPeers != 1 || len(p.queue) != 2 {
		t.Fatalf("expired requeue: %+v %v", got, err)
	}
}
