// SPDX-License-Identifier: GPL-3.0-or-later
// Offline synthetic compatibility proof only. Not a live-chain importer.
package checkpointproof

import (
	"bytes"
	"crypto/ecdsa"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"math/big"
	"reflect"
	"sort"
	"testing"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/consensus/misc/eip1559"
	"github.com/ethereum/go-ethereum/consensus/parlia"
	"github.com/ethereum/go-ethereum/core/rawdb"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/crypto"
	"github.com/ethereum/go-ethereum/ethdb"
	"github.com/ethereum/go-ethereum/params"
	"github.com/ethereum/go-ethereum/rlp"
	"github.com/prysmaticlabs/prysm/v5/crypto/bls"
)

type chain struct {
	cfg           *params.ChainConfig
	head          *types.Header
	byNumber      map[uint64]*types.Header
	byHash        map[common.Hash]*types.Header
	reads, misses int
}

func (c *chain) Config() *params.ChainConfig  { return c.cfg }
func (c *chain) CurrentHeader() *types.Header { return c.head }
func (c *chain) GetHeader(h common.Hash, n uint64) *types.Header {
	c.reads++
	x := c.byHash[h]
	if x == nil || x.Number.Uint64() != n {
		c.misses++
		return nil
	}
	return x
}
func (c *chain) GetHeaderByNumber(n uint64) *types.Header           { return c.byNumber[n] }
func (c *chain) GetHeaderByHash(h common.Hash) *types.Header        { return c.byHash[h] }
func (c *chain) GenesisHeader() *types.Header                       { return c.byNumber[0] }
func (c *chain) GetTd(common.Hash, uint64) *big.Int                 { return nil }
func (c *chain) GetHighestVerifiedHeader() *types.Header            { return c.head }
func (c *chain) GetVerifiedBlockByHash(h common.Hash) *types.Header { return c.byHash[h] }
func (c *chain) add(h *types.Header) {
	c.byNumber[h.Number.Uint64()] = h
	c.byHash[h.Hash()] = h
	c.head = h
}
func (c *chain) slice(first, last uint64) *chain {
	x := &chain{cfg: c.cfg, byNumber: map[uint64]*types.Header{}, byHash: map[common.Hash]*types.Header{}}
	x.add(c.GenesisHeader())
	for n := first; n <= last; n++ {
		x.add(c.byNumber[n])
	}
	return x
}
func api(p *parlia.Parlia, c *chain) *parlia.API { return p.APIs(c)[0].Service.(*parlia.API) }
func snapshot(t *testing.T, p *parlia.Parlia, c *chain, h *types.Header) *parlia.Snapshot {
	t.Helper()
	s, err := api(p, c).GetSnapshotAtHash(h.Hash())
	if err != nil {
		t.Fatal(err)
	}
	return s
}
func encoded(t *testing.T, s *parlia.Snapshot) []byte {
	t.Helper()
	b, err := json.Marshal(s)
	if err != nil {
		t.Fatal(err)
	}
	return b
}
func store(t *testing.T, db ethdb.Database, s *parlia.Snapshot) {
	t.Helper()
	if err := db.Put(append([]byte("parlia-"), s.Hash[:]...), encoded(t, s)); err != nil {
		t.Fatal(err)
	}
}

type fixture struct {
	chain  *chain
	db     ethdb.Database
	engine *parlia.Parlia
	keys   []*ecdsa.PrivateKey
	votes  []bls.SecretKey
}

func makeFixture(t *testing.T) *fixture {
	t.Helper()
	zero := uint64(0)
	// Deliberately synthetic fork schedule, not BSC mainnet or Chapel.
	cfg := &params.ChainConfig{ChainID: big.NewInt(98765), Parlia: &params.ParliaConfig{}, RamanujanBlock: big.NewInt(0), LondonBlock: big.NewInt(0), LubanBlock: big.NewInt(0), PlatoBlock: big.NewInt(0), BohrTime: &zero, LorentzTime: &zero, MaxwellTime: &zero, FermiTime: &zero}
	f := &fixture{chain: &chain{cfg: cfg, byNumber: map[uint64]*types.Header{}, byHash: map[common.Hash]*types.Header{}}, db: rawdb.NewMemoryDatabase()}
	for i := byte(1); i <= 3; i++ {
		b := make([]byte, 32)
		b[31] = i
		k, e := crypto.ToECDSA(b)
		if e != nil {
			t.Fatal(e)
		}
		f.keys = append(f.keys, k)
	}
	sort.Slice(f.keys, func(i, j int) bool {
		return bytes.Compare(crypto.PubkeyToAddress(f.keys[i].PublicKey).Bytes(), crypto.PubkeyToAddress(f.keys[j].PublicKey).Bytes()) < 0
	})
	for i := byte(11); i <= 13; i++ {
		b := make([]byte, 32)
		b[31] = i
		k, e := bls.SecretKeyFromBytes(b)
		if e != nil {
			t.Fatal(e)
		}
		f.votes = append(f.votes, k)
	}
	g := &types.Header{Number: big.NewInt(0), Difficulty: big.NewInt(1), BaseFee: big.NewInt(0), GasLimit: 40000000, Time: 1600000000, UncleHash: types.EmptyUncleHash, Extra: f.extra(true, nil), ParentBeaconRoot: new(common.Hash)}
	f.chain.add(g)
	f.engine = parlia.New(cfg, f.db, nil, g.Hash())
	t.Cleanup(func() { f.engine.Close(); f.db.Close() })
	return f
}
func (f *fixture) extra(epoch bool, a *types.VoteAttestation) []byte {
	x := make([]byte, 32)
	if epoch {
		x = append(x, byte(len(f.keys)))
		for i, k := range f.keys {
			x = append(x, crypto.PubkeyToAddress(k.PublicKey).Bytes()...)
			x = append(x, f.votes[i].PublicKey().Marshal()...)
		}
		x = append(x, 32)
	}
	if a != nil {
		b, e := rlp.EncodeToBytes(a)
		if e != nil {
			panic(e)
		}
		x = append(x, b...)
	}
	return append(x, make([]byte, 65)...)
}
func (f *fixture) next(t *testing.T, targetDepth uint64) *types.Header {
	t.Helper()
	p := f.chain.head
	n := p.Number.Uint64() + 1
	s := snapshot(t, f.engine, f.chain, p)
	var a *types.VoteAttestation
	if n > 2 && targetDepth > 0 {
		source := uint64(0)
		sourceHash := f.chain.GenesisHeader().Hash()
		if s.Attestation != nil {
			source = s.Attestation.TargetNumber
			sourceHash = s.Attestation.TargetHash
		}
		target := f.chain.byNumber[n-targetDepth]
		a = &types.VoteAttestation{VoteAddressSet: 7, Data: &types.VoteData{SourceNumber: source, SourceHash: sourceHash, TargetNumber: target.Number.Uint64(), TargetHash: target.Hash()}}
		h := a.Data.Hash()
		sigs := []bls.Signature{}
		for _, k := range f.votes {
			sigs = append(sigs, k.Sign(h[:]))
		}
		copy(a.AggSignature[:], bls.AggregateSignatures(sigs).Marshal())
	}
	k := f.keys[(n/uint64(s.TurnLength))%uint64(len(f.keys))]
	h := &types.Header{Number: new(big.Int).SetUint64(n), ParentHash: p.Hash(), Difficulty: big.NewInt(2), BaseFee: eip1559.CalcBaseFee(f.chain.cfg, p), GasLimit: p.GasLimit, Time: p.Time + 3, UncleHash: types.EmptyUncleHash, Extra: f.extra(n%s.EpochLength == 0, a), Coinbase: crypto.PubkeyToAddress(k.PublicKey), ParentBeaconRoot: new(common.Hash)}
	sig, e := crypto.Sign(types.SealHash(h, f.chain.cfg.ChainID).Bytes(), k)
	if e != nil {
		t.Fatal(e)
	}
	copy(h.Extra[len(h.Extra)-65:], sig)
	return h
}
func (f *fixture) advance(t *testing.T, end uint64) {
	f.advanceWithTarget(t, end, 1)
}

func (f *fixture) advanceWithTarget(t *testing.T, end, targetDepth uint64) {
	t.Helper()
	for f.chain.head.Number.Uint64() < end {
		h := f.next(t, targetDepth)
		if err := f.engine.VerifyHeader(f.chain, h); err != nil {
			t.Fatalf("header %d: %v", h.Number, err)
		}
		f.chain.add(h)
	}
}
func fresh(t *testing.T, c *chain, snaps ...*parlia.Snapshot) (*parlia.Parlia, ethdb.Database) {
	t.Helper()
	db := rawdb.NewMemoryDatabase()
	for _, s := range snaps {
		store(t, db, s)
	}
	p := parlia.New(c.cfg, db, nil, c.GenesisHeader().Hash())
	t.Cleanup(func() { p.Close(); db.Close() })
	return p, db
}

func TestCheckpointRoundTripAndDependencies(t *testing.T) {
	f := makeFixture(t)
	f.advance(t, 2048)
	support := snapshot(t, f.engine, f.chain, f.chain.head)
	f.advance(t, 3072)
	anchor := snapshot(t, f.engine, f.chain, f.chain.head)
	candidate := f.next(t, 1)
	b := encoded(t, anchor)
	for _, literal := range []string{`"attestation:omitempty":`, `"index:omitempty":`, `"recent_fork_hashes":`, `"vote_address":`} {
		if !bytes.Contains(b, []byte(literal)) {
			t.Fatalf("missing versioned literal %s", literal)
		}
	}
	foundSentinel := false
	for n, v := range support.Recents {
		if n > support.Number && v == (common.Address{}) {
			foundSentinel = true
		}
	}
	if !foundSentinel {
		t.Fatal("test must exercise Bohr epoch sentinel")
	}
	pruned := f.chain.slice(3069, 3072)
	p, _ := fresh(t, pruned, anchor)
	got := snapshot(t, p, pruned, pruned.head)
	if !bytes.Equal(encoded(t, got), b) {
		t.Fatal("snapshot round trip differs")
	}
	if err := p.VerifyHeader(pruned, candidate); err == nil {
		t.Fatal("one anchor unexpectedly supplied target-parent snapshot")
	} else {
		t.Logf("anchor-only attestation rejection: %v", err)
	}
	// Including the complete support snapshot and the hash-linked suffix allows
	// reconstruction of target-parent state without genesis-to-head history.
	// Epoch 2000 is needed when the delayed set switch applies at 2063.
	bounded := f.chain.slice(2000, 3072)
	derivedEngine, _ := fresh(t, bounded, support)
	derived := snapshot(t, derivedEngine, bounded, bounded.head)
	if !bytes.Equal(encoded(t, derived), b) {
		t.Fatal("support replay does not reproduce checkpoint snapshot")
	}
	p, db := fresh(t, bounded, support, anchor)
	if err := p.VerifyHeader(bounded, candidate); err != nil {
		t.Fatalf("bounded bootstrap: %v", err)
	}
	bounded.add(candidate)
	got = snapshot(t, p, bounded, candidate)
	f.chain.add(candidate)
	want := snapshot(t, f.engine, f.chain, candidate)
	if !bytes.Equal(encoded(t, got), encoded(t, want)) {
		t.Fatal("resumed state differs from full synthetic replay")
	}
	justified, jhash, err := p.GetJustifiedNumberAndHash(bounded, []*types.Header{candidate})
	if err != nil || justified != 3072 || jhash != candidate.ParentHash {
		t.Fatalf("justified %d %s %v", justified, jhash, err)
	}
	finalized := p.GetFinalizedHeader(bounded, candidate)
	if finalized == nil || finalized.Number.Uint64() != 3071 {
		t.Fatalf("unexpected finalized header %v", finalized)
	}
	if p.VotePool != nil {
		t.Fatal("vote pool must remain nil")
	}
	iterator := db.NewIterator([]byte("parlia-"), nil)
	defer iterator.Release()
	count := 0
	for iterator.Next() {
		count++
	}
	if err := iterator.Error(); err != nil {
		t.Fatal(err)
	}
	if count != 2 {
		t.Fatalf("unexpected snapshot records: %d", count)
	}
	t.Logf("bounded proof: headers=%d, snapshots=%d, anchorBytes=%d, reads=%d, misses=%d", len(bounded.byHash), count, len(b), bounded.reads, bounded.misses)
	broken := f.chain.slice(2001, 3072)
	p, _ = fresh(t, broken, support, anchor)
	if err := p.VerifyHeader(broken, candidate); err == nil {
		t.Fatal("missing epoch ancestor accepted")
	} else {
		t.Logf("missing epoch dependency rejected: %v", err)
	}
	// A non-aligned record is ignored by upstream's on-disk snapshot lookup.
	unmatched := f.chain.slice(3073, 3073)
	p, _ = fresh(t, unmatched, want)
	if _, err := api(p, unmatched).GetSnapshotAtHash(candidate.Hash()); err == nil {
		t.Fatal("nonaligned snapshot unexpectedly loaded")
	}
	// Pause votes so a three-generation target can still exceed the latest
	// justified block. Fermi permits depth 3, but not depth 4.
	branch := f.chain.slice(2000, 3068)
	bp, bdb := fresh(t, branch, support)
	bf := &fixture{chain: branch, db: bdb, engine: bp, keys: f.keys, votes: f.votes}
	for branch.head.Number.Uint64() < 3072 {
		h := bf.next(t, 0)
		if err := bp.VerifyHeader(branch, h); err != nil {
			t.Fatal(err)
		}
		branch.add(h)
	}
	ba := snapshot(t, bp, branch, branch.head)
	three := bf.next(t, 3)
	rp, _ := fresh(t, branch, support, ba)
	if err := rp.VerifyHeader(branch, three); err != nil {
		t.Fatalf("Fermi depth 3: %v", err)
	}
	four := bf.next(t, 4)
	if err := rp.VerifyHeader(branch, four); err == nil {
		t.Fatal("Fermi depth 4 accepted")
	}
	// Mutate an aggregate BLS signature and re-sign the proposer commitment.
	var att types.VoteAttestation
	if err := rlp.DecodeBytes(three.Extra[32:len(three.Extra)-65], &att); err != nil {
		t.Fatal(err)
	}
	att.AggSignature[0] ^= 1
	bad := types.CopyHeader(three)
	bad.Extra = bf.extra(false, &att)
	key := bf.keys[(bad.Number.Uint64()/uint64(ba.TurnLength))%uint64(len(bf.keys))]
	sig, err := crypto.Sign(types.SealHash(bad, branch.cfg.ChainID).Bytes(), key)
	if err != nil {
		t.Fatal(err)
	}
	copy(bad.Extra[len(bad.Extra)-65:], sig)
	if err := rp.VerifyHeader(branch, bad); err == nil {
		t.Fatal("invalid BLS aggregate accepted")
	}
}

func TestHeaderHashDoesNotAuthenticateSnapshot(t *testing.T) {
	f := makeFixture(t)
	f.advanceWithTarget(t, 1024, 0)
	s := snapshot(t, f.engine, f.chain, f.chain.head)
	original := encoded(t, s)
	trustedBundleDigest := sha256.Sum256(original)
	var forged parlia.Snapshot
	if err := json.Unmarshal(original, &forged); err != nil {
		t.Fatal(err)
	}
	forged.Recents = map[uint64]common.Address{}
	if forged.Hash != s.Hash {
		t.Fatal("header hash changed")
	}
	b := encoded(t, &forged)
	if sha256.Sum256(b) == trustedBundleDigest {
		t.Fatal("authentication test failed")
	}
	c := f.chain.slice(1024, 1024)
	p, _ := fresh(t, c, &forged)
	loaded := snapshot(t, p, c, c.head)
	if !reflect.DeepEqual(loaded.Recents, forged.Recents) {
		t.Fatal("expected upstream loader to trust forged record")
	}
	t.Log("unchanged header hash does not stop upstream loading forged empty recent-signing state; authenticate the full bundle before writing DB")
}

// This exercises one stable modern epoch replacement, not a proof that every
// historical/fork-transition snapshot can exclude departed validators.
func TestModernRecentsAcrossValidatorReplacement(t *testing.T) {
	f := makeFixture(t)
	// This test concerns proposer history, not quorum validation (covered above).
	// Omit votes throughout to retain recency and avoid thousands of redundant
	// BLS aggregate checks in the repository-wide race suite.
	f.advanceWithTarget(t, 1999, 0)
	replacement := &fixture{}
	for i := byte(101); i <= 103; i++ {
		b := make([]byte, 32)
		b[31] = i
		k, err := crypto.ToECDSA(b)
		if err != nil {
			t.Fatal(err)
		}
		replacement.keys = append(replacement.keys, k)
	}
	sort.Slice(replacement.keys, func(i, j int) bool {
		return bytes.Compare(crypto.PubkeyToAddress(replacement.keys[i].PublicKey).Bytes(), crypto.PubkeyToAddress(replacement.keys[j].PublicKey).Bytes()) < 0
	})
	for i := byte(111); i <= 113; i++ {
		b := make([]byte, 32)
		b[31] = i
		k, err := bls.SecretKeyFromBytes(b)
		if err != nil {
			t.Fatal(err)
		}
		replacement.votes = append(replacement.votes, k)
	}
	old := map[common.Address]bool{}
	for _, k := range f.keys {
		old[crypto.PubkeyToAddress(k.PublicKey)] = true
	}
	for n := uint64(2000); n <= 2063; n++ {
		// No new votes here: recency is not erased by advancing finality.
		h := f.next(t, 0)
		if n == 2000 {
			h.Extra = replacement.extra(true, nil)
			s := snapshot(t, f.engine, f.chain, f.chain.head)
			key := f.keys[(n/uint64(s.TurnLength))%uint64(len(f.keys))]
			sig, err := crypto.Sign(types.SealHash(h, f.chain.cfg.ChainID).Bytes(), key)
			if err != nil {
				t.Fatal(err)
			}
			copy(h.Extra[len(h.Extra)-65:], sig)
		}
		if err := f.engine.VerifyHeader(f.chain, h); err != nil {
			t.Fatalf("header %d: %v", n, err)
		}
		f.chain.add(h)
		if n == 2062 {
			s := snapshot(t, f.engine, f.chain, h)
			normal := 0
			for height, addr := range s.Recents {
				if height <= n && old[addr] {
					normal++
				}
			}
			if normal == 0 {
				t.Fatal("fixture did not retain old-validator history before switch")
			}
		}
	}
	if !old[f.chain.head.Coinbase] {
		t.Fatal("switch header was not signed by an old validator")
	}
	s := snapshot(t, f.engine, f.chain, f.chain.head)
	for addr := range s.Validators {
		if old[addr] {
			t.Fatal("old validator remains in new set")
		}
	}
	sentinel, ok := s.Recents[^uint64(0)-2]
	if len(s.Recents) != 1 || !ok || sentinel != (common.Address{}) {
		t.Fatal("Bohr switch did not clear ordinary history and retain epoch sentinel")
	}
	f.keys, f.votes = replacement.keys, replacement.votes
	h := f.next(t, 0)
	if err := f.engine.VerifyHeader(f.chain, h); err != nil {
		t.Fatal(err)
	}
	f.chain.add(h)
	s = snapshot(t, f.engine, f.chain, h)
	for height, addr := range s.Recents {
		if height > s.Number {
			continue
		}
		if _, ok := s.Validators[addr]; !ok {
			t.Fatalf("departed validator retained at %d", height)
		}
	}
}

func Example_privateRecordKey() {
	var hash common.Hash
	fmt.Printf("%x\n", append([]byte("parlia-"), hash[:]...))
	// Output: 7061726c69612d0000000000000000000000000000000000000000000000000000000000000000
}
