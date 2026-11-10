// SPDX-License-Identifier: GPL-3.0-or-later
package relay

import (
	"context"
	"errors"
	"fmt"
	"math/big"
	"sync"
	"sync/atomic"
	"time"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/forkid"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/crypto"
	eth "github.com/ethereum/go-ethereum/eth/protocols/eth"
	"github.com/ethereum/go-ethereum/p2p"
	"github.com/ethereum/go-ethereum/p2p/enode"
	"github.com/ethereum/go-ethereum/params"
	"github.com/ethereum/go-ethereum/rlp"
)

const maxMessage = 10 * 1024 * 1024
const maxRequestMessage = 2 * 1024 * 1024
const maxHandshakeMessage = 64 * 1024

var ErrNoPeers = errors.New("no compatible transaction-receiving peer is ready")
var ErrQueuesFull = errors.New("all peer queues are full; retry later")
var ErrAdmissionExpired = errors.New("transaction expired during admission; retry later")

type Config struct {
	Network, ListenAddr              string
	Peers                            []string
	NoDiscovery                      bool
	MaxPeers, CacheCount, CacheBytes int
	CacheTTL                         time.Duration
}

func DefaultConfig() Config {
	return Config{Network: "mainnet", ListenAddr: ":30311", MaxPeers: 20, CacheCount: 1024, CacheBytes: 16 * 1024 * 1024, CacheTTL: 2 * time.Minute}
}

type outbound struct {
	code    uint64
	data    any
	expires time.Time // Zero for protocol responses, fixed cache deadline for transactions.
}
type pooledReply struct {
	requestID uint64
	hashes    []common.Hash
}

type peerSession struct {
	sentNextSweep time.Time // Protected by Node.submitMu.
	peer          *p2p.Peer
	rw            p2p.MsgReadWriter
	queue         chan outbound
	done          chan struct{}
	receiveTx     bool
	sent          map[common.Hash]time.Time // Protected by Node.submitMu; bounded by cache admission + TTL.
}
type Node struct {
	submitMu      sync.Mutex
	cfg           Config
	chain         *chainView
	cache         *store
	server        *p2p.Server
	mu            sync.RWMutex
	peers         map[enode.ID]*peerSession
	stop          chan struct{}
	stopOnce      sync.Once
	writes        atomic.Uint64
	writeFailures atomic.Uint64
}

type Submission struct {
	Hash        common.Hash    `json:"hash"`
	Sender      common.Address `json:"sender"`
	QueuedPeers int            `json:"queued_peers"`
	Duplicate   bool           `json:"duplicate"`
	Stage       string         `json:"stage"`
}
type Health struct {
	Network              string `json:"network"`
	ChainID              string `json:"chain_id"`
	ReadyPeers           int    `json:"ready_peers"`
	ConnectedPeers       int    `json:"connected_peers"`
	CachedTransactions   int    `json:"cached_transactions"`
	CachedBytes          int    `json:"cached_bytes"`
	WrittenMessages      uint64 `json:"written_messages"`
	WriteFailures        uint64 `json:"write_failures"`
	ConfirmationTracking bool   `json:"confirmation_tracking"`
}

func New(cfg Config) (*Node, error) {
	if cfg.MaxPeers < 1 || cfg.MaxPeers > 200 || cfg.CacheCount < 1 || cfg.CacheBytes < MaxRawTransaction || cfg.CacheTTL <= 0 {
		return nil, errors.New("invalid peer or cache limits")
	}
	chain, err := network(cfg.Network)
	if err != nil {
		return nil, err
	}
	key, err := crypto.GenerateKey()
	if err != nil {
		return nil, err
	}
	n := &Node{cfg: cfg, chain: chain, cache: newStore(cfg.CacheCount, cfg.CacheBytes, cfg.CacheTTL), peers: make(map[enode.ID]*peerSession), stop: make(chan struct{})}
	var peers, boots []*enode.Node
	for _, url := range cfg.Peers {
		peer, err := enode.Parse(enode.ValidSchemes, url)
		if err != nil {
			return nil, fmt.Errorf("invalid peer: %w", err)
		}
		peers = append(peers, peer)
	}
	if !cfg.NoDiscovery && cfg.Network == "mainnet" {
		for _, url := range params.MainnetBootnodes {
			peer, err := enode.Parse(enode.ValidSchemes, url)
			if err != nil {
				return nil, err
			}
			boots = append(boots, peer)
		}
	}
	if cfg.Network == "testnet" && len(peers) == 0 {
		return nil, errors.New("testnet requires at least one explicit --peer; no mainnet bootnodes are used")
	}
	n.server = &p2p.Server{Config: p2p.Config{PrivateKey: key, Name: "bsc-p2p-relay/0.1.0", MaxPeers: cfg.MaxPeers, MaxPeersPerIP: 4, MaxPendingPeers: 8, DialRatio: 1, ListenAddr: cfg.ListenAddr, NoDiscovery: cfg.NoDiscovery, DiscoveryV4: !cfg.NoDiscovery, BootstrapNodes: boots, StaticNodes: peers, NodeDatabase: "", Protocols: []p2p.Protocol{{Name: "eth", Version: 68, Length: 17, Run: n.runPeer}}}}
	return n, nil
}
func (n *Node) Start() error {
	if err := n.server.Start(); err != nil {
		return err
	}
	go func() {
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()
		for {
			select {
			case now := <-ticker.C:
				n.prune(now)
			case <-n.stop:
				return
			}
		}
	}()
	return nil
}
func (n *Node) Close()        { n.stopOnce.Do(func() { close(n.stop); n.server.Stop() }) }
func (n *Node) Enode() string { return n.server.Self().URLv4() }
func (n *Node) Health() Health {
	n.mu.RLock()
	ready, connected := 0, len(n.peers)
	for _, p := range n.peers {
		if p.receiveTx {
			ready++
		}
	}
	n.mu.RUnlock()
	count, bytes := n.cache.stats()
	return Health{n.cfg.Network, n.chain.config.ChainID.String(), ready, connected, count, bytes, n.writes.Load(), n.writeFailures.Load(), false}
}

// Submit reports bounded local queue admission, never peer acceptance/inclusion.
func (n *Node) Submit(raw []byte) (Submission, error) {
	tx, sender, err := validate(raw, n.chain.config.ChainID)
	if err != nil {
		return Submission{}, err
	}
	result := Submission{Hash: tx.Hash(), Sender: sender, Stage: "queued"}
	n.submitMu.Lock()
	defer n.submitMu.Unlock()
	n.mu.RLock()
	defer n.mu.RUnlock()
	available := false
	for _, p := range n.peers {
		if p.receiveTx {
			available = true
			break
		}
	}
	if !available {
		return result, ErrNoPeers
	}
	fresh, err := n.cache.add(tx, time.Now())
	if err != nil {
		return result, err
	}
	result.Duplicate = !fresh
	now := time.Now()
	expires := n.cache.expiry(tx.Hash())
	if expires.IsZero() || !now.Before(expires) {
		return result, ErrAdmissionExpired
	}
	alreadyQueued := 0
	eligible := 0
	for _, p := range n.peers {
		if !p.receiveTx {
			continue
		}
		select {
		case <-p.done:
			continue
		default:
		}
		if p.sent == nil {
			p.sent = make(map[common.Hash]time.Time)
		}
		// Sweep at most once per second, unless the hard count bound needs
		// space. The target entry is still checked exactly on every access.
		if !now.Before(p.sentNextSweep) {
			p.pruneSent(now)
		}
		if deadline, ok := p.sent[tx.Hash()]; ok {
			if now.Before(deadline) {
				alreadyQueued++
				continue
			}
			delete(p.sent, tx.Hash())
		}
		eligible++
		// Only a new admission needs capacity. A valid duplicate above must
		// not rescan a full map on every call.
		if len(p.sent) >= n.cfg.CacheCount {
			p.pruneSent(now)
		}
		if len(p.sent) >= n.cfg.CacheCount {
			continue
		}
		select {
		case p.queue <- outbound{code: eth.TransactionsMsg, data: types.Transactions{tx}, expires: expires}:
			p.sent[tx.Hash()] = expires
			result.QueuedPeers++
		default:
		}
	}
	if result.QueuedPeers == 0 {
		result.Stage = "cached"
		if eligible > 0 || alreadyQueued == 0 {
			if fresh {
				n.cache.remove(tx.Hash())
			}
			return result, ErrQueuesFull
		}
		result.Duplicate = true
	}
	return result, nil
}

func (n *Node) handshake(p *peerSession) error {
	local := eth.StatusPacket68{ProtocolVersion: 68, NetworkID: n.chain.config.ChainID.Uint64(), TD: big.NewInt(1), Head: n.chain.genesis.Hash(), Genesis: n.chain.genesis.Hash(), ForkID: forkid.NewIDWithChain(n.chain)}
	var remote eth.StatusPacket68
	if err := exchange(p, eth.StatusMsg, &local, &remote); err != nil {
		return err
	}
	if remote.ProtocolVersion != 68 || remote.NetworkID != local.NetworkID || remote.Genesis != local.Genesis {
		return errors.New("incompatible network, genesis or protocol")
	}
	if remote.TD == nil || remote.TD.BitLen() > 100 {
		return errors.New("invalid total difficulty")
	}
	if err := forkid.NewStaticFilter(n.chain.config, n.chain.genesis)(remote.ForkID); err != nil {
		return err
	}
	extension, err := (&eth.UpgradeStatusExtension{DisablePeerTxBroadcast: true}).Encode()
	if err != nil {
		return err
	}
	var upgrade eth.UpgradeStatusPacket
	if err := exchange(p, eth.UpgradeStatusMsg, &eth.UpgradeStatusPacket{Extension: extension}, &upgrade); err != nil {
		return err
	}
	remoteExtension, err := upgrade.GetExtension()
	if err != nil {
		return err
	}
	p.receiveTx = !remoteExtension.DisablePeerTxBroadcast
	return nil
}

func exchange(p *peerSession, code uint64, local, remote any) error {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	results := make(chan error, 2)
	go func() { results <- p2p.Send(p.rw, code, local) }()
	go func() {
		msg, err := p.rw.ReadMsg()
		if err != nil {
			results <- err
			return
		}
		if msg.Size > maxHandshakeMessage {
			results <- errors.New("handshake message too large")
			return
		}
		defer msg.Discard()
		if msg.Code != code {
			results <- fmt.Errorf("expected handshake code %d, got %d", code, msg.Code)
			return
		}
		results <- msg.Decode(remote)
	}()
	for i := 0; i < 2; i++ {
		select {
		case err := <-results:
			if err != nil {
				return err
			}
		case <-ctx.Done():
			p.peer.Disconnect(p2p.DiscReadTimeout)
			return ctx.Err()
		}
	}
	return nil
}
func (n *Node) runPeer(peer *p2p.Peer, rw p2p.MsgReadWriter) error {
	p := &peerSession{peer: peer, rw: rw, queue: make(chan outbound, 64), done: make(chan struct{})}
	if err := n.handshake(p); err != nil {
		return err
	}
	n.mu.Lock()
	n.peers[peer.ID()] = p
	n.mu.Unlock()
	defer func() { close(p.done); n.mu.Lock(); delete(n.peers, peer.ID()); n.mu.Unlock() }()
	go n.writer(p)
	for {
		msg, err := rw.ReadMsg()
		if err != nil {
			return err
		}
		if msg.Size > maxMessage {
			return errors.New("peer message exceeds eth 10 MiB limit")
		}
		err = n.handle(p, msg)
		_ = msg.Discard()
		if err != nil {
			return err
		}
	}
}
func (n *Node) writer(p *peerSession) {
	for {
		select {
		case <-p.done:
			return
		case <-n.stop:
			return
		case out := <-p.queue:
			if out.code == eth.TransactionsMsg && (out.expires.IsZero() || !time.Now().Before(out.expires)) {
				continue
			}
			if err := p2p.Send(p.rw, out.code, n.outboundData(out)); err != nil {
				n.writeFailures.Add(1)
				p.peer.Disconnect(p2p.DiscNetworkError)
				return
			}
			n.writes.Add(1)
		}
	}
}
func enqueue(p *peerSession, code uint64, value any) error {
	select {
	case p.queue <- outbound{code: code, data: value}:
		return nil
	case <-p.done:
		return errors.New("peer disconnected")
	default:
		return errors.New("peer response queue full")
	}
}

// Empty responses honestly represent data this relay does not possess.
func (n *Node) handle(p *peerSession, msg p2p.Msg) error {
	if (msg.Code == eth.GetBlockHeadersMsg || msg.Code == eth.GetBlockBodiesMsg || msg.Code == eth.GetReceiptsMsg || msg.Code == eth.GetPooledTransactionsMsg) && msg.Size > maxRequestMessage {
		return errors.New("request exceeds 2 MiB decode limit")
	}
	switch msg.Code {
	case eth.GetBlockHeadersMsg:
		var q eth.GetBlockHeadersPacket
		if err := msg.Decode(&q); err != nil {
			return err
		}
		return enqueue(p, eth.BlockHeadersMsg, []any{q.RequestId, []rlp.RawValue{}})
	case eth.GetBlockBodiesMsg, eth.GetReceiptsMsg:
		var q struct {
			RequestID uint64
			Hashes    []common.Hash
		}
		if err := msg.Decode(&q); err != nil {
			return err
		}
		if len(q.Hashes) > 1024 {
			return errors.New("too many requested hashes")
		}
		code := uint64(eth.BlockBodiesMsg)
		if msg.Code == eth.GetReceiptsMsg {
			code = eth.ReceiptsMsg
		}
		return enqueue(p, code, []any{q.RequestID, []rlp.RawValue{}})
	case eth.GetPooledTransactionsMsg:
		var q struct {
			RequestID uint64
			Hashes    []common.Hash
		}
		if err := msg.Decode(&q); err != nil {
			return err
		}
		if len(q.Hashes) > 256 {
			return errors.New("too many requested transactions")
		}
		return enqueue(p, eth.PooledTransactionsMsg, pooledReply{requestID: q.RequestID, hashes: q.Hashes})
	case eth.StatusMsg, eth.UpgradeStatusMsg:
		return errors.New("unexpected repeated handshake")
	case eth.NewBlockHashesMsg, eth.TransactionsMsg, eth.BlockHeadersMsg, eth.BlockBodiesMsg, eth.NewBlockMsg, eth.NewPooledTransactionHashesMsg, eth.PooledTransactionsMsg, eth.ReceiptsMsg:
		// No peer payload is cached, acted upon, or represented as verified.
		return nil
	default:
		return fmt.Errorf("unsupported eth/68 message code %d", msg.Code)
	}
}

// Resolve requested transactions only when the writer is ready, so response
// queues do not retain transaction bodies or send entries expired while waiting.
func (n *Node) outboundData(out outbound) any {
	reply, ok := out.data.(pooledReply)
	if !ok {
		return out.data
	}
	var txs types.Transactions
	size := 0
	now := time.Now()
	for _, h := range reply.hashes {
		if tx := n.cache.get(h, now); tx != nil {
			if size+int(tx.Size()) > maxRequestMessage/2 {
				break
			}
			txs = append(txs, tx)
			size += int(tx.Size())
		}
	}
	return []any{reply.requestID, txs}
}

// Periodic maintenance also releases expired per-peer hash metadata while idle.
func (n *Node) prune(now time.Time) {
	n.cache.prune(now)
	n.submitMu.Lock()
	defer n.submitMu.Unlock()
	n.mu.RLock()
	defer n.mu.RUnlock()
	for _, p := range n.peers {
		p.pruneSent(now)
	}
}

// pruneSent requires the owning node's submitMu.
func (p *peerSession) pruneSent(now time.Time) {
	for hash, deadline := range p.sent {
		if !now.Before(deadline) {
			delete(p.sent, hash)
		}
	}
	p.sentNextSweep = now.Add(time.Second)
}
