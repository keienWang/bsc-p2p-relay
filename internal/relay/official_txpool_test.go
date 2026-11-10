// SPDX-License-Identifier: GPL-3.0-or-later
//go:build officialtxpool

package relay

import (
	"crypto/ecdsa"
	"errors"
	"math/big"
	"net"
	"testing"
	"time"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core"
	"github.com/ethereum/go-ethereum/core/rawdb"
	"github.com/ethereum/go-ethereum/core/txpool"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/crypto"
	bsceth "github.com/ethereum/go-ethereum/eth"
	"github.com/ethereum/go-ethereum/eth/ethconfig"
	"github.com/ethereum/go-ethereum/node"
	"github.com/ethereum/go-ethereum/p2p"
	"github.com/ethereum/go-ethereum/p2p/netutil"
	"github.com/ethereum/go-ethereum/params"
)

// TestOfficialBSCTxPoolLoopback runs the pinned BSC backend, official network
// handler, state-backed blockchain and real transaction pools in this process.
// It does not launch the official binary or exercise any public network. Public
// test keys below are intentionally worthless and only funded in this genesis.
func TestOfficialBSCTxPoolLoopback(t *testing.T) {
	funded := officialTxPoolKey(t, "0000000000000000000000000000000000000000000000000000000000000001")
	unfunded := officialTxPoolKey(t, "0000000000000000000000000000000000000000000000000000000000000002")
	address := crypto.PubkeyToAddress(funded.PublicKey)

	// Copy the upstream configuration rather than mutating a global shared by
	// other tests. All supported envelope forks are active; Parlia selects the
	// actual BSC state/transaction rules. No blocks are sealed or imported.
	chainConfig := *params.AllEthashProtocolChanges
	chainConfig.ChainID = big.NewInt(13371337)
	chainConfig.Ethash = nil
	chainConfig.Parlia = &params.ParliaConfig{}
	extra := make([]byte, 32+common.AddressLength+65)
	copy(extra[32:], address[:])
	genesis := &core.Genesis{
		Config: &chainConfig, Timestamp: 1, GasLimit: 30_000_000,
		Difficulty: big.NewInt(1), BaseFee: big.NewInt(params.InitialBaseFee), ExtraData: extra,
		Alloc: types.GenesisAlloc{address: {Balance: new(big.Int).Exp(big.NewInt(10), big.NewInt(21), nil)}},
	}
	loopback, err := netutil.ParseNetlist("127.0.0.0/8")
	if err != nil {
		t.Fatal(err)
	}
	// Do not start from node.DefaultConfig: it includes discovery and NAT.
	stack, err := node.New(&node.Config{
		Name: "bsc-relay-private-txpool-test", DataDir: t.TempDir(),
		P2P: p2p.Config{ListenAddr: "127.0.0.1:0", MaxPeers: 2, MaxPeersPerIP: 2,
			NoDiscovery: true, NoDial: true, NetRestrict: loopback},
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := stack.Close(); err != nil {
			t.Errorf("close official BSC stack: %v", err)
		}
	})
	config := ethconfig.Defaults
	config.Genesis = genesis
	config.NetworkId = chainConfig.ChainID.Uint64()
	config.SyncMode = ethconfig.FullSync
	config.DatabaseCache, config.DatabaseHandles = 16, 16
	config.TrieCleanCache, config.TrieDirtyCache, config.SnapshotCache = 16, 16, 0
	config.StateScheme = rawdb.HashScheme
	config.LogNoHistory, config.DisableTxIndexer, config.DisableSnapProtocol = true, true, true
	config.EthDiscoveryURLs, config.SnapDiscoveryURLs, config.BscDiscoveryURLs = nil, nil, nil
	config.TxPool.Journal, config.TxPool.NoLocals = "", true
	config.BlobPool.Datacap = 16 * 1024 * 1024
	config.Miner.BidBlockRevokesJournal = ""
	config.Miner.Mev.GRPCDisabled = true
	backend, err := bsceth.New(stack, &config)
	if err != nil {
		t.Fatal(err)
	}
	if err := stack.Start(); err != nil {
		t.Fatal(err)
	}
	// The private fixture has the entire chain (genesis). Explicitly enable the
	// official handler's post-sync transaction path, not mining or an RPC API.
	backend.SetSynced()
	if !backend.Synced() || backend.IsMining() {
		t.Fatal("private backend must be synced and not mining")
	}
	if stack.IPCEndpoint() != "" || stack.HTTPEndpoint() != "http://" || stack.WSEndpoint() != "ws://" ||
		stack.HTTPAuthEndpoint() != "http://" || stack.WSAuthEndpoint() != "ws://" {
		t.Fatal("test unexpectedly enabled an RPC endpoint")
	}
	officialTxPoolAssertIsolation(t, stack.Server())
	if !stack.Server().NoDial || len(config.EthDiscoveryURLs)+len(config.SnapDiscoveryURLs)+len(config.BscDiscoveryURLs) != 0 || !config.Miner.Mev.GRPCDisabled {
		t.Fatal("official backend unexpectedly enables dialing, DNS discovery, or MEV gRPC")
	}

	n := protocolNode(t)
	// Only test code can select this synthetic genesis. Production network
	// configuration remains limited to mainnet/testnet with no override.
	n.chain = &chainView{config: &chainConfig, genesis: genesis.ToBlock()}
	n.server.NetRestrict = loopback
	if err := n.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(n.Close)
	officialTxPoolAssertIsolation(t, n.server)
	if len(n.cfg.Peers) != 0 || n.chain.config.ChainID.Int64() == 56 || n.chain.config.ChainID.Int64() == 97 || n.chain.genesis.Hash() == params.BSCGenesisHash || n.chain.genesis.Hash() == params.ChapelGenesisHash {
		t.Fatal("test fixture must not select public peers or a public BSC chain")
	}
	n.server.AddPeer(stack.Server().Self())
	officialTxPoolAwait(t, "official BSC eth/68 handshake", func() bool { return n.Health().ReadyPeers == 1 })
	pool := backend.TxPool()

	// Valid signatures alone do not establish acceptance: only the real pool's
	// pending status and contents below count as success.
	for nonce, kind := range []uint8{types.LegacyTxType, types.AccessListTxType, types.DynamicFeeTxType} {
		tx := officialTxPoolTransaction(t, funded, chainConfig.ChainID, uint64(nonce), kind)
		sub := officialTxPoolSubmit(t, n, tx)
		if sub.QueuedPeers != 1 || sub.Duplicate {
			t.Fatalf("fresh queue admission: %+v", sub)
		}
		officialTxPoolAwait(t, "official pool acceptance", func() bool { return pool.Status(tx.Hash()) == txpool.TxStatusPending })
		if got := pool.Get(tx.Hash()); got == nil || got.Hash() != tx.Hash() || got.Type() != kind {
			t.Fatalf("official pool did not retain type %d transaction", kind)
		}
	}
	first := officialTxPoolTransaction(t, funded, chainConfig.ChainID, 0, types.LegacyTxType)
	duplicate := officialTxPoolSubmit(t, n, first)
	if !duplicate.Duplicate || duplicate.QueuedPeers != 0 {
		t.Fatalf("same-session duplicate was not suppressed: %+v", duplicate)
	}
	if pending, queued := pool.Stats(); pending != 3 || queued != 0 {
		t.Fatalf("official pool stats after duplicate = (%d, %d), want (3, 0)", pending, queued)
	}

	// A locally valid signed transaction with no balance is admitted to the
	// relay but rejected by the stateful official pool. A subsequent valid
	// transaction is a processing barrier: the official handler calls pool.Add
	// synchronously in wire order before moving to the next TransactionsMsg.
	rejected := officialTxPoolTransaction(t, unfunded, chainConfig.ChainID, 0, types.LegacyTxType)
	if sub := officialTxPoolSubmit(t, n, rejected); sub.QueuedPeers != 1 {
		t.Fatalf("unfunded transaction did not enter relay queue: %+v", sub)
	}
	barrier := officialTxPoolTransaction(t, funded, chainConfig.ChainID, 3, types.LegacyTxType)
	officialTxPoolSubmit(t, n, barrier)
	officialTxPoolAwait(t, "valid transaction after rejected transaction", func() bool { return pool.Status(barrier.Hash()) == txpool.TxStatusPending })
	if pool.Has(rejected.Hash()) || pool.Status(rejected.Hash()) != txpool.TxStatusUnknown {
		t.Fatal("official pool accepted an unfunded transaction")
	}
	// The wire protocol has no rejection-reason response. After proving absence
	// through P2P, ask the same real pool directly only for the rejected fixture
	// to verify the exact state validation error. No successful Add is performed
	// by the test; every accepted transaction arrives exclusively over RLPx.
	failures := pool.Add([]*types.Transaction{rejected}, true)
	if len(failures) != 1 || !errors.Is(failures[0], core.ErrInsufficientFunds) {
		t.Fatalf("stateful rejection = %v, want insufficient funds", failures)
	}

	// Disconnect the same remote node, then reconnect and retry an already
	// cached transaction. The relay retries per-session; the official pool
	// remains unique by hash. A fresh transaction proves the new path works.
	// Use a transient transport error, not RemovePeer/DiscRequested. BSC
	// intentionally remembers explicit disconnect requests and will not
	// readmit that identity. Static-peer redial handles this error normally.
	peers := n.server.Peers()
	if len(peers) != 1 {
		t.Fatalf("expected one peer before disconnect, got %d", len(peers))
	}
	peers[0].Disconnect(p2p.DiscNetworkError)
	officialTxPoolAwait(t, "disconnect", func() bool { return n.Health().ReadyPeers == 0 && stack.Server().PeerCount() == 0 })
	officialTxPoolAwait(t, "reconnect", func() bool { return n.Health().ReadyPeers == 1 })
	retry := officialTxPoolSubmit(t, n, first)
	if !retry.Duplicate || retry.QueuedPeers != 1 {
		t.Fatalf("cached transaction not retried on new session: %+v", retry)
	}
	last := officialTxPoolTransaction(t, funded, chainConfig.ChainID, 4, types.DynamicFeeTxType)
	officialTxPoolSubmit(t, n, last)
	officialTxPoolAwait(t, "acceptance after reconnect", func() bool { return pool.Status(last.Hash()) == txpool.TxStatusPending })
	pending, queued := pool.Content()
	if len(pending) != 1 || len(pending[address]) != 5 || len(queued) != 0 || pool.Has(rejected.Hash()) {
		t.Fatalf("unexpected final official pool contents: pending senders=%d funded txs=%d queued senders=%d", len(pending), len(pending[address]), len(queued))
	}
	for i, tx := range pending[address] {
		if tx.Nonce() != uint64(i) {
			t.Fatalf("pending nonce %d at position %d", tx.Nonce(), i)
		}
	}
	if backend.BlockChain().CurrentBlock().Number.Sign() != 0 || backend.IsMining() {
		t.Fatal("fixture must remain unmined at its private genesis")
	}
}

func officialTxPoolAssertIsolation(t *testing.T, server *p2p.Server) {
	t.Helper()
	host, _, err := net.SplitHostPort(server.ListenAddr)
	if err != nil || host != "127.0.0.1" || !server.Self().IP().Equal(net.IPv4(127, 0, 0, 1)) {
		t.Fatalf("RLPx listener is not IPv4 localhost: %q", server.ListenAddr)
	}
	if !server.NoDiscovery || server.DiscoveryV4() != nil || server.DiscoveryV5() != nil || server.NAT != nil {
		t.Fatal("test server unexpectedly enables discovery or NAT")
	}
	if len(server.BootstrapNodes)+len(server.BootstrapNodesV5)+len(server.StaticNodes)+len(server.TrustedNodes) != 0 {
		t.Fatal("test server has preconfigured bootstrap/static/trusted peers")
	}
	if server.NetRestrict == nil || !server.NetRestrict.Contains(net.IPv4(127, 0, 0, 1)) || server.NetRestrict.Contains(net.IPv4(1, 1, 1, 1)) {
		t.Fatal("test server is not restricted to loopback peers")
	}
}

func officialTxPoolKey(t *testing.T, hex string) *ecdsa.PrivateKey {
	t.Helper()
	key, err := crypto.HexToECDSA(hex)
	if err != nil {
		t.Fatal(err)
	}
	return key
}

func officialTxPoolTransaction(t *testing.T, key *ecdsa.PrivateKey, chainID *big.Int, nonce uint64, kind uint8) *types.Transaction {
	t.Helper()
	to := common.Address{19: 0x42}
	price := big.NewInt(2 * params.GWei)
	var inner types.TxData
	switch kind {
	case types.LegacyTxType:
		inner = &types.LegacyTx{Nonce: nonce, To: &to, Value: big.NewInt(1), Gas: 21000, GasPrice: price}
	case types.AccessListTxType:
		inner = &types.AccessListTx{ChainID: chainID, Nonce: nonce, To: &to, Value: big.NewInt(1), Gas: 21000, GasPrice: price}
	case types.DynamicFeeTxType:
		inner = &types.DynamicFeeTx{ChainID: chainID, Nonce: nonce, To: &to, Value: big.NewInt(1), Gas: 21000, GasTipCap: price, GasFeeCap: price}
	default:
		t.Fatalf("unsupported fixture type %d", kind)
	}
	tx, err := types.SignTx(types.NewTx(inner), types.LatestSignerForChainID(chainID), key)
	if err != nil {
		t.Fatal(err)
	}
	return tx
}

func officialTxPoolSubmit(t *testing.T, n *Node, tx *types.Transaction) Submission {
	t.Helper()
	raw, err := tx.MarshalBinary()
	if err != nil {
		t.Fatal(err)
	}
	sub, err := n.Submit(raw)
	if err != nil {
		t.Fatal(err)
	}
	return sub
}

func officialTxPoolAwait(t *testing.T, what string, ready func() bool) {
	t.Helper()
	// Upstream RLPx imposes a 35-second dial-history cooldown, including
	// static peers. Allow it to expire naturally; do not bypass peer throttles.
	deadline := time.NewTimer(45 * time.Second)
	defer deadline.Stop()
	ticker := time.NewTicker(10 * time.Millisecond)
	defer ticker.Stop()
	for !ready() {
		select {
		case <-ticker.C:
		case <-deadline.C:
			t.Fatalf("timed out waiting for %s", what)
		}
	}
}
