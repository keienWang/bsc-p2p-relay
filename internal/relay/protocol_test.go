// SPDX-License-Identifier: GPL-3.0-or-later
package relay

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"math/big"
	"strings"
	"testing"
	"time"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/forkid"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/crypto"
	eth "github.com/ethereum/go-ethereum/eth/protocols/eth"
	"github.com/ethereum/go-ethereum/p2p"
	"github.com/ethereum/go-ethereum/p2p/enode"
	"github.com/ethereum/go-ethereum/rlp"
)

func protocolNode(t *testing.T) *Node {
	t.Helper()
	cfg := DefaultConfig()
	cfg.NoDiscovery = true
	cfg.ListenAddr = "127.0.0.1:0"
	n, err := New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	return n
}

func awaitProtocol(t *testing.T, result <-chan error) {
	t.Helper()
	select {
	case err := <-result:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("local protocol operation timed out")
	}
}

// These signatures use fresh ephemeral keys, exist only in test memory, and are
// never sent to a public network. They exercise wire encodings, not payments.
func protocolTransactions(t *testing.T) types.Transactions {
	t.Helper()
	key, err := crypto.GenerateKey()
	if err != nil {
		t.Fatal(err)
	}
	chain := big.NewInt(56)
	to := common.Address{19: 1}
	unsigned := []*types.Transaction{
		types.NewTx(&types.LegacyTx{Nonce: 1, To: &to, Gas: 21000, GasPrice: big.NewInt(1)}),
		types.NewTx(&types.AccessListTx{ChainID: chain, Nonce: 2, To: &to, Gas: 21000, GasPrice: big.NewInt(1)}),
		types.NewTx(&types.DynamicFeeTx{ChainID: chain, Nonce: 3, To: &to, Gas: 21000, GasTipCap: big.NewInt(1), GasFeeCap: big.NewInt(2)}),
	}
	var signed types.Transactions
	for _, tx := range unsigned {
		s, err := types.SignTx(tx, types.LatestSignerForChainID(chain), key)
		if err != nil {
			t.Fatal(err)
		}
		signed = append(signed, s)
	}
	return signed
}

func TestOfficialBSC68Handshake(t *testing.T) {
	for _, disable := range []bool{false, true} {
		t.Run(fmt.Sprintf("disable_remote_broadcast_%t", disable), func(t *testing.T) {
			n := protocolNode(t)
			a, b := p2p.MsgPipe()
			defer a.Close()
			defer b.Close()
			local := &peerSession{peer: p2p.NewPeer(enode.ID{1}, "relay", nil), rw: a, queue: make(chan outbound, 8), done: make(chan struct{})}
			official := eth.NewPeer(68, p2p.NewPeer(enode.ID{2}, "official-bsc", nil), b, nil, n.chain.config)
			defer official.Close()
			result := make(chan error, 1)
			go func() { result <- n.handshake(local) }()
			if err := official.Handshake(56, n.chain, eth.BlockRangeUpdatePacket{}, big.NewInt(1), &eth.UpgradeStatusExtension{DisablePeerTxBroadcast: disable}); err != nil {
				t.Fatal(err)
			}
			awaitProtocol(t, result)
			if local.receiveTx == disable {
				t.Fatalf("receiveTx=%t, remote disable=%t", local.receiveTx, disable)
			}
			n.peers[local.peer.ID()] = local
			raw, err := protocolTransactions(t)[0].MarshalBinary()
			if err != nil {
				t.Fatal(err)
			}
			sub, err := n.Submit(raw)
			if disable {
				if !errors.Is(err, ErrNoPeers) {
					t.Fatalf("wanted ErrNoPeers, got %v", err)
				}
				if len(local.queue) != 0 {
					t.Fatal("queued a transaction despite remote opt-out")
				}
			} else if err != nil || sub.QueuedPeers != 1 {
				t.Fatalf("submission=%+v err=%v", sub, err)
			}
		})
	}
}

func TestTransactionWireEncodingOfficialBSC(t *testing.T) {
	txs := protocolTransactions(t)
	a, b := p2p.MsgPipe()
	defer a.Close()
	defer b.Close()
	result := make(chan error, 1)
	go func() { result <- p2p.Send(a, eth.TransactionsMsg, txs) }()
	msg, err := b.ReadMsg()
	if err != nil {
		t.Fatal(err)
	}
	defer msg.Discard()
	var got eth.TransactionsPacket
	if err := msg.Decode(&got); err != nil {
		t.Fatal(err)
	}
	decoded, err := got.RawList.Items()
	if err != nil {
		t.Fatal(err)
	}
	if len(decoded) != len(txs) {
		t.Fatalf("got %d transactions, want %d", len(decoded), len(txs))
	}
	for i, tx := range decoded {
		if tx.Hash() != txs[i].Hash() || tx.Type() != txs[i].Type() {
			t.Fatalf("transaction %d changed on wire", i)
		}
	}
	awaitProtocol(t, result)
}

func TestEmptyBlockResponsesPreserveRequestID(t *testing.T) {
	n := protocolNode(t)
	const id = uint64(0x123456789abcdef)
	tests := []struct {
		name       string
		code, want uint64
		packet     any
	}{
		{"headers", eth.GetBlockHeadersMsg, eth.BlockHeadersMsg, &eth.GetBlockHeadersPacket{RequestId: id, GetBlockHeadersRequest: &eth.GetBlockHeadersRequest{Origin: eth.HashOrNumber{Number: 1}, Amount: 10}}},
		{"bodies", eth.GetBlockBodiesMsg, eth.BlockBodiesMsg, &eth.GetBlockBodiesPacket{RequestId: id, GetBlockBodiesRequest: eth.GetBlockBodiesRequest{common.Hash{1}}}},
		{"receipts", eth.GetReceiptsMsg, eth.ReceiptsMsg, &eth.GetReceiptsPacket68{RequestId: id, GetReceiptsRequest: eth.GetReceiptsRequest{common.Hash{1}}}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			payload, err := rlp.EncodeToBytes(tt.packet)
			if err != nil {
				t.Fatal(err)
			}
			p := &peerSession{queue: make(chan outbound, 1), done: make(chan struct{})}
			if err := n.handle(p, p2p.Msg{Code: tt.code, Size: uint32(len(payload)), Payload: bytes.NewReader(payload)}); err != nil {
				t.Fatal(err)
			}
			out := <-p.queue
			if out.code != tt.want {
				t.Fatalf("response code %d, want %d", out.code, tt.want)
			}
			encoded, err := rlp.EncodeToBytes(n.outboundData(out))
			if err != nil {
				t.Fatal(err)
			}
			var response struct {
				RequestID uint64
				Items     []rlp.RawValue
			}
			if err := rlp.DecodeBytes(encoded, &response); err != nil {
				t.Fatal(err)
			}
			if response.RequestID != id || len(response.Items) != 0 {
				t.Fatalf("unexpected response %+v", response)
			}
		})
	}
}

// Both RLPx listeners are loopback-only and discovery is disabled. The remote
// endpoint uses the pinned official BSC handshake and packet decoder.
func TestLoopbackRLPxOfficialBSC(t *testing.T) {
	n := protocolNode(t)
	result := make(chan error, 1)
	received := make(chan common.Hash, 1)
	key, err := crypto.GenerateKey()
	if err != nil {
		t.Fatal(err)
	}
	server := &p2p.Server{Config: p2p.Config{PrivateKey: key, Name: "official-bsc-test", MaxPeers: 2, NoDiscovery: true, ListenAddr: "127.0.0.1:0", Protocols: []p2p.Protocol{{Name: "eth", Version: 68, Length: 17, Run: func(p *p2p.Peer, rw p2p.MsgReadWriter) error {
		peer := eth.NewPeer(68, p, rw, nil, n.chain.config)
		defer peer.Close()
		if err := peer.Handshake(56, n.chain, eth.BlockRangeUpdatePacket{}, big.NewInt(1), nil); err != nil {
			result <- err
			return err
		}
		msg, err := rw.ReadMsg()
		if err != nil {
			result <- err
			return err
		}
		defer msg.Discard()
		var packet eth.TransactionsPacket
		var txs []*types.Transaction
		if msg.Code != eth.TransactionsMsg {
			err = fmt.Errorf("unexpected code %d", msg.Code)
		} else {
			err = msg.Decode(&packet)
			if err == nil {
				txs, err = packet.RawList.Items()
			}
		}
		if err == nil && len(txs) != 1 {
			err = fmt.Errorf("got %d transactions", len(txs))
		}
		if err == nil {
			received <- txs[0].Hash()
		}
		result <- err
		if err != nil {
			return err
		}
		_, err = rw.ReadMsg()
		return err
	}}}}}
	if err := server.Start(); err != nil {
		t.Fatal(err)
	}
	defer server.Stop()
	if err := n.Start(); err != nil {
		t.Fatal(err)
	}
	defer n.Close()
	n.server.AddPeer(server.Self())
	deadline := time.Now().Add(10 * time.Second)
	for n.Health().ReadyPeers == 0 {
		select {
		case err := <-result:
			t.Fatalf("remote stopped: %v", err)
		default:
		}
		if time.Now().After(deadline) {
			t.Fatal("local RLPx peer did not become ready")
		}
		time.Sleep(10 * time.Millisecond)
	}
	tx := protocolTransactions(t)[0]
	raw, err := tx.MarshalBinary()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := n.Submit(raw); err != nil {
		t.Fatal(err)
	}
	awaitProtocol(t, result)
	select {
	case hash := <-received:
		if hash != tx.Hash() {
			t.Fatalf("hash %s, want %s", hash, tx.Hash())
		}
	default:
		t.Fatal("missing transaction")
	}
}

func TestPooledTransactionResponseOfficialBSC(t *testing.T) {
	n := protocolNode(t)
	txs := protocolTransactions(t)
	var hashes []common.Hash
	for _, tx := range txs {
		if _, err := n.cache.add(tx, time.Now()); err != nil {
			t.Fatal(err)
		}
		hashes = append(hashes, tx.Hash())
	}
	hashes = append(hashes, common.Hash{31: 255}) // Unknown hashes are omitted.
	request := &eth.GetPooledTransactionsPacket{RequestId: 91, GetPooledTransactionsRequest: hashes}
	payload, err := rlp.EncodeToBytes(request)
	if err != nil {
		t.Fatal(err)
	}
	p := &peerSession{queue: make(chan outbound, 1), done: make(chan struct{})}
	if err := n.handle(p, p2p.Msg{Code: eth.GetPooledTransactionsMsg, Size: uint32(len(payload)), Payload: bytes.NewReader(payload)}); err != nil {
		t.Fatal(err)
	}
	out := <-p.queue
	if out.code != eth.PooledTransactionsMsg {
		t.Fatalf("wrong response code %d", out.code)
	}
	payload, err = rlp.EncodeToBytes(n.outboundData(out))
	if err != nil {
		t.Fatal(err)
	}
	var got eth.PooledTransactionsPacket
	if err := rlp.DecodeBytes(payload, &got); err != nil {
		t.Fatal(err)
	}
	decoded, err := got.List.Items()
	if err != nil {
		t.Fatal(err)
	}
	if got.RequestId != 91 || len(decoded) != len(txs) {
		t.Fatalf("request ID=%d, transactions=%d", got.RequestId, len(decoded))
	}
	for i, tx := range decoded {
		if tx.Hash() != txs[i].Hash() {
			t.Fatalf("transaction %d changed", i)
		}
	}
}

func TestHandshakeRejectsWrongNetwork(t *testing.T) {
	n := protocolNode(t)
	a, b := p2p.MsgPipe()
	defer a.Close()
	defer b.Close()
	p := &peerSession{peer: p2p.NewPeer(enode.ID{3}, "wrong-network", nil), rw: a}
	result := make(chan error, 1)
	go func() { result <- n.handshake(p) }()
	msg, err := b.ReadMsg()
	if err != nil {
		t.Fatal(err)
	}
	var status eth.StatusPacket68
	if err := msg.Decode(&status); err != nil {
		t.Fatal(err)
	}
	if err := msg.Discard(); err != nil {
		t.Fatal(err)
	}
	status.NetworkID++
	if err := p2p.Send(b, eth.StatusMsg, &status); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-result:
		if err == nil {
			t.Fatal("accepted incorrect network")
		}
	case <-time.After(10 * time.Second):
		t.Fatal("handshake failed to reject wrong network")
	}
}

// syntheticForkChain is only an EIP-2124 fixture. Its header is fabricated:
// neither a downloaded chain head nor a consensus-validated BSC block.
type syntheticForkChain struct {
	*chainView
	header *types.Header
}

func (c *syntheticForkChain) CurrentHeader() *types.Header { return c.header }

func TestOfficialBSC68SyntheticJennerForkHandshake(t *testing.T) {
	n := protocolNode(t)
	if n.chain.config.JennerTime == nil {
		t.Fatal("pinned mainnet config has no Jenner activation")
	}
	activation := *n.chain.config.JennerTime
	const syntheticNumber = uint64(100_000_000)
	before := forkid.NewID(n.chain.config, n.chain.genesis, syntheticNumber, activation-1)
	after := forkid.NewID(n.chain.config, n.chain.genesis, syntheticNumber, activation+1)
	if before.Hash == after.Hash || before.Next != activation {
		t.Fatal("fixture does not straddle Jenner fork")
	}
	for _, tc := range []struct {
		name      string
		timestamp uint64
	}{
		{"before_jenner", activation - 1}, {"after_jenner", activation + 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			header := n.chain.genesis.Header()
			header.Number = new(big.Int).SetUint64(syntheticNumber)
			header.Time = tc.timestamp
			fixture := &syntheticForkChain{chainView: n.chain, header: header}
			a, b := p2p.MsgPipe()
			defer a.Close()
			defer b.Close()
			local := &peerSession{peer: p2p.NewPeer(enode.ID{4}, "genesis-relay", nil), rw: a}
			official := eth.NewPeer(68, p2p.NewPeer(enode.ID{5}, "synthetic-modern-bsc", nil), b, nil, n.chain.config)
			defer official.Close()
			result := make(chan error, 1)
			go func() { result <- n.handshake(local) }()
			if err := official.Handshake(56, fixture, eth.BlockRangeUpdatePacket{}, new(big.Int).SetUint64(syntheticNumber), nil); err != nil {
				t.Fatal(err)
			}
			awaitProtocol(t, result)
			if !local.receiveTx {
				t.Fatal("default official peer not marked ready for transactions")
			}
			remoteHead, _ := official.Head()
			if remoteHead != n.chain.genesis.Hash() {
				t.Fatalf("relay advertised %s instead of honest genesis %s", remoteHead, n.chain.genesis.Hash())
			}
			if remoteHead == header.Hash() {
				t.Fatal("relay copied synthetic peer head")
			}
		})
	}
}

// This scripted transport avoids allocating an oversized payload and ensures
// rejection is based on the frame size before any payload decoding occurs.
type protocolInbound struct{ msg p2p.Msg }

func (p *protocolInbound) ReadMsg() (p2p.Msg, error) { return p.msg, nil }
func (p *protocolInbound) WriteMsg(msg p2p.Msg) error {
	_, err := io.Copy(io.Discard, msg.Payload)
	return err
}

type unreadableProtocolPayload struct{}

func (unreadableProtocolPayload) Read([]byte) (int, error) {
	return 0, errors.New("oversized payload must not be read")
}

func TestHandshakeRejectsInvalidFrames(t *testing.T) {
	for _, tc := range []struct {
		name string
		msg  p2p.Msg
		want string
	}{
		{"oversized_status", p2p.Msg{Code: eth.StatusMsg, Size: maxHandshakeMessage + 1, Payload: unreadableProtocolPayload{}}, "handshake message too large"},
		{"upgrade_before_status", p2p.Msg{Code: eth.UpgradeStatusMsg, Size: 1, Payload: bytes.NewReader([]byte{0xc0})}, "expected handshake code"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			n := protocolNode(t)
			p := &peerSession{peer: p2p.NewPeer(enode.ID{6}, "invalid-frame", nil), rw: &protocolInbound{msg: tc.msg}}
			err := n.handshake(p)
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("got %v, want %q", err, tc.want)
			}
		})
	}
}

func TestRepeatedHandshakeRejected(t *testing.T) {
	n := protocolNode(t)
	for _, code := range []uint64{eth.StatusMsg, eth.UpgradeStatusMsg} {
		t.Run(fmt.Sprintf("code_%d", code), func(t *testing.T) {
			p := &peerSession{queue: make(chan outbound, 1), done: make(chan struct{})}
			err := n.handle(p, p2p.Msg{Code: code, Payload: bytes.NewReader(nil)})
			if err == nil || !strings.Contains(err.Error(), "unexpected repeated handshake") {
				t.Fatalf("got %v", err)
			}
			if len(p.queue) != 0 {
				t.Fatal("queued response to repeated handshake")
			}
		})
	}
}

func TestRunPeerSurvivesLargeDiscardedBlock(t *testing.T) {
	n := protocolNode(t)
	a, b := p2p.MsgPipe()
	defer a.Close()
	defer b.Close()
	relayDone := make(chan error, 1)
	go func() { relayDone <- n.runPeer(p2p.NewPeer(enode.ID{7}, "large-block-relay", nil), a) }()
	official := eth.NewPeer(68, p2p.NewPeer(enode.ID{8}, "large-block-official-fixture", nil), b, nil, n.chain.config)
	defer official.Close()
	if err := official.Handshake(56, n.chain, eth.BlockRangeUpdatePacket{}, big.NewInt(1), nil); err != nil {
		t.Fatal(err)
	}

	// A deliberately fabricated, consensus-invalid header supplies a large RLP
	// block envelope. The relay must drain this notification without interpreting
	// or trusting its contents; this is not a real block or chain-head fixture.
	block := types.NewBlockWithHeader(&types.Header{
		Number: big.NewInt(1), Difficulty: big.NewInt(1), Extra: make([]byte, 3*1024*1024),
	})
	payload, err := rlp.EncodeToBytes(&eth.NewBlockPacket{Block: block, TD: big.NewInt(2)})
	if err != nil {
		t.Fatal(err)
	}
	if len(payload) <= 2*1024*1024 || len(payload) > 10*1024*1024 {
		t.Fatalf("bad regression fixture size: %d", len(payload))
	}
	const requestID = uint64(0xfeed123456)
	remoteDone := make(chan error, 1)
	go func() {
		if err := b.WriteMsg(p2p.Msg{Code: eth.NewBlockMsg, Size: uint32(len(payload)), Payload: bytes.NewReader(payload)}); err != nil {
			remoteDone <- err
			return
		}
		query := &eth.GetBlockHeadersPacket{RequestId: requestID, GetBlockHeadersRequest: &eth.GetBlockHeadersRequest{Origin: eth.HashOrNumber{Number: 1}, Amount: 1}}
		if err := p2p.Send(b, eth.GetBlockHeadersMsg, query); err != nil {
			remoteDone <- err
			return
		}
		msg, err := b.ReadMsg()
		if err != nil {
			remoteDone <- err
			return
		}
		defer msg.Discard()
		if msg.Code != eth.BlockHeadersMsg {
			remoteDone <- fmt.Errorf("response code %d, want BlockHeaders", msg.Code)
			return
		}
		var response eth.BlockHeadersPacket
		if err := msg.Decode(&response); err != nil {
			remoteDone <- err
			return
		}
		headers, err := response.List.Items()
		if err != nil {
			remoteDone <- err
			return
		}
		if response.RequestId != requestID || len(headers) != 0 {
			remoteDone <- fmt.Errorf("response ID=%d headers=%d, want ID=%d and empty", response.RequestId, len(headers), requestID)
			return
		}
		remoteDone <- nil
	}()
	select {
	case err := <-remoteDone:
		if err != nil {
			t.Fatal(err)
		}
	case err := <-relayDone:
		t.Fatalf("runPeer disconnected after large block: %v", err)
	case <-time.After(10 * time.Second):
		t.Fatal("no header response after large block")
	}
	if n.Health().ConnectedPeers != 1 {
		t.Fatal("relay session did not survive discarded block")
	}
	// Closing the private transport must release runPeer as well as its writer.
	a.Close()
	select {
	case <-relayDone:
	case <-time.After(10 * time.Second):
		t.Fatal("runPeer failed to stop after transport close")
	}
}
