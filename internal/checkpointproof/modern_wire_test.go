// SPDX-License-Identifier: GPL-3.0-or-later
package checkpointproof

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"math/big"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"testing"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/consensus/misc/eip4844"
	"github.com/ethereum/go-ethereum/consensus/parlia"
	"github.com/ethereum/go-ethereum/core"
	"github.com/ethereum/go-ethereum/core/rawdb"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/crypto"
	"github.com/ethereum/go-ethereum/params"
	"github.com/ethereum/go-ethereum/rlp"
	"github.com/prysmaticlabs/prysm/v5/crypto/bls"
)

// modernWireConfig is a separate, synthetic all-forks-through-Fermi profile.
// It is neither a real BSC network configuration nor an update to makeFixture.
// The pinned official Parlia test configuration supplies the complete earlier
// forks through Cancun. CheckConfigForkOrder requires every intervening fork,
// including Prague, before Lorentz/Maxwell/Fermi may be enabled.
func modernWireConfig(t *testing.T) *params.ChainConfig {
	t.Helper()
	b, err := json.Marshal(params.ParliaTestChainConfig)
	if err != nil {
		t.Fatal(err)
	}
	var cfg params.ChainConfig
	if err := json.Unmarshal(b, &cfg); err != nil {
		t.Fatal(err)
	}
	cfg.ChainID = big.NewInt(98765)
	for _, dst := range []**uint64{&cfg.HaberTime, &cfg.HaberFixTime, &cfg.BohrTime, &cfg.PascalTime, &cfg.PragueTime, &cfg.LorentzTime, &cfg.MaxwellTime, &cfg.FermiTime} {
		*dst = new(uint64)
	}
	prague := *params.DefaultPragueBlobConfigBSC
	cfg.BlobScheduleConfig.Prague = &prague
	if err := cfg.CheckConfigForkOrder(); err != nil {
		t.Fatalf("modern wire fork order: %v", err)
	}
	return &cfg
}

type modernWireFixture struct{ *fixture }

func makeModernWireFixture(t *testing.T) *modernWireFixture {
	t.Helper()
	cfg := modernWireConfig(t)
	f := &fixture{chain: &chain{cfg: cfg, byNumber: map[uint64]*types.Header{}, byHash: map[common.Hash]*types.Header{}}, db: rawdb.NewMemoryDatabase()}
	// These public test-only private scalars are deliberately identical to the
	// legacy fixture, but this is a newly constructed and signed chain.
	for i := byte(1); i <= 3; i++ {
		b := make([]byte, 32)
		b[31] = i
		key, err := crypto.ToECDSA(b)
		if err != nil {
			t.Fatal(err)
		}
		f.keys = append(f.keys, key)
	}
	sort.Slice(f.keys, func(i, j int) bool {
		return bytes.Compare(crypto.PubkeyToAddress(f.keys[i].PublicKey).Bytes(), crypto.PubkeyToAddress(f.keys[j].PublicKey).Bytes()) < 0
	})
	for i := byte(11); i <= 13; i++ {
		b := make([]byte, 32)
		b[31] = i
		key, err := bls.SecretKeyFromBytes(b)
		if err != nil {
			t.Fatal(err)
		}
		f.votes = append(f.votes, key)
	}
	// Genesis.ToBlock applies the pinned official BSC genesis rules: Cancun
	// empty withdrawals and zero blob fields, Bohr zero beacon root, Prague
	// empty requests, and BSC's London base fee. No state transition runs.
	genesis := (&core.Genesis{Config: cfg, Timestamp: 1600000000, GasLimit: 40000000, Difficulty: big.NewInt(1), ExtraData: f.extra(true, nil)}).ToBlock().Header()
	genesis = canonicalWireHeader(t, genesis)
	f.chain.add(genesis)
	f.engine = parlia.New(cfg, f.db, nil, genesis.Hash())
	t.Cleanup(func() { f.engine.Close(); f.db.Close() })
	return &modernWireFixture{f}
}

func (f *modernWireFixture) next(t *testing.T, targetDepth uint64) *types.Header {
	t.Helper()
	// Reuse the public test validator/vote construction, then replace the
	// legacy optional fields and re-sign the complete modern seal commitment.
	h := f.fixture.next(t, targetDepth)
	h.Root = types.EmptyRootHash // synthetic placeholder, NOT an executed state root
	h.TxHash, h.ReceiptHash = types.EmptyTxsHash, types.EmptyReceiptsHash
	withdrawals, requests := types.EmptyWithdrawalsHash, types.EmptyRequestsHash
	h.WithdrawalsHash, h.RequestsHash = &withdrawals, &requests
	h.BlobGasUsed = new(uint64) // no blob transactions in this synthetic fixture
	excess := eip4844.CalcExcessBlobGas(f.chain.cfg, f.chain.head, h.Time)
	h.ExcessBlobGas = &excess
	h.ParentBeaconRoot = new(common.Hash)
	f.sign(t, h)
	return h
}

func (f *modernWireFixture) sign(t *testing.T, h *types.Header) {
	t.Helper()
	for _, key := range f.keys {
		if crypto.PubkeyToAddress(key.PublicKey) == h.Coinbase {
			sig, err := crypto.Sign(types.SealHash(h, f.chain.cfg.ChainID).Bytes(), key)
			if err != nil {
				t.Fatal(err)
			}
			copy(h.Extra[len(h.Extra)-65:], sig)
			return
		}
	}
	t.Fatal("no fixture proposer key")
}

func (f *modernWireFixture) advance(t *testing.T, end uint64) {
	t.Helper()
	for f.chain.head.Number.Uint64() < end {
		h := canonicalWireHeader(t, f.next(t, 1))
		if err := f.engine.VerifyHeader(f.chain, h); err != nil {
			t.Fatalf("modern wire header %d: %v", h.Number, err)
		}
		// Only a decoded, completely verified header enters the reader.
		f.chain.add(h)
	}
}

// headerSemanticDifference compares ALL exported Header fields and preserves
// nil pointer/slice states. big.Int values use nil checks then Cmp; reflect's
// comparison of internal nil versus empty machine-word slices is not semantic.
func headerSemanticDifference(a, b *types.Header) string {
	if a == nil || b == nil {
		if a != b {
			return "Header nil"
		}
		return ""
	}
	av, bv := reflect.ValueOf(*a), reflect.ValueOf(*b)
	bigType := reflect.TypeOf((*big.Int)(nil))
	for i := 0; i < av.NumField(); i++ {
		x, y := av.Field(i), bv.Field(i)
		name := av.Type().Field(i).Name
		if x.Type() == bigType {
			xi, yi := x.Interface().(*big.Int), y.Interface().(*big.Int)
			if xi == nil || yi == nil {
				if xi != yi {
					return name
				}
				continue
			}
			if xi.Cmp(yi) != 0 {
				return name
			}
		} else if !reflect.DeepEqual(x.Interface(), y.Interface()) {
			return name
		}
	}
	return ""
}

func canonicalWireHeader(t *testing.T, h *types.Header) *types.Header {
	t.Helper()
	wire, err := rlp.EncodeToBytes(h)
	if err != nil {
		t.Fatal(err)
	}
	var got types.Header
	if err := rlp.DecodeBytes(wire, &got); err != nil {
		t.Fatalf("header %d RLP decode: %v", h.Number, err)
	}
	again, err := rlp.EncodeToBytes(&got)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(wire, again) {
		t.Fatal("noncanonical header RLP round trip")
	}
	if field := headerSemanticDifference(h, &got); field != "" {
		t.Fatalf("RLP changed header %d field %s", h.Number, field)
	}
	if h.Hash() != got.Hash() {
		t.Fatal("RLP changed header hash")
	}
	copy := types.CopyHeader(&got)
	if field := headerSemanticDifference(&got, copy); field != "" {
		t.Fatalf("CopyHeader changed %s", field)
	}
	// Every populated pointer and slice must have independent mutable storage.
	gv, cv := reflect.ValueOf(got), reflect.ValueOf(*copy)
	for i := 0; i < gv.NumField(); i++ {
		x, y := gv.Field(i), cv.Field(i)
		if (x.Kind() == reflect.Pointer || x.Kind() == reflect.Slice) && !x.IsNil() && x.Pointer() == y.Pointer() {
			t.Fatalf("CopyHeader aliases %s", gv.Type().Field(i).Name)
		}
	}
	copy.Difficulty.Add(copy.Difficulty, big.NewInt(1))
	copy.Number.Add(copy.Number, big.NewInt(1))
	copy.BaseFee.Add(copy.BaseFee, big.NewInt(1))
	copy.Extra[0] ^= 1
	(*copy.WithdrawalsHash)[0] ^= 1
	(*copy.BlobGasUsed)++
	(*copy.ExcessBlobGas)++
	(*copy.ParentBeaconRoot)[0] ^= 1
	(*copy.RequestsHash)[0] ^= 1
	if field := headerSemanticDifference(h, &got); field != "" || h.Hash() != got.Hash() {
		t.Fatalf("copy mutation changed original: %s", field)
	}
	return &got
}

func TestModernWireSmallChain(t *testing.T) {
	f := makeModernWireFixture(t)
	cfg := f.chain.cfg
	if !cfg.IsShanghai(common.Big0, f.chain.head.Time) || !cfg.IsCancun(common.Big0, f.chain.head.Time) || !cfg.IsPrague(common.Big0, f.chain.head.Time) || !cfg.IsFermi(common.Big0, f.chain.head.Time) {
		t.Fatal("modern profile inactive")
	}
	if cfg.IsOsaka(common.Big0, f.chain.head.Time) || cfg.IsAmsterdam(common.Big0, f.chain.head.Time) {
		t.Fatal("unexpected later fork")
	}
	f.advance(t, 12)
	candidate := canonicalWireHeader(t, f.next(t, 1))
	if err := f.engine.VerifyHeader(f.chain, candidate); err != nil {
		t.Fatal(err)
	}
	for name, mutate := range map[string]func(*types.Header){
		"withdrawals":        func(h *types.Header) { h.WithdrawalsHash = new(common.Hash) },
		"blob-gas":           func(h *types.Header) { *h.BlobGasUsed = 1 },
		"excess-blob-gas":    func(h *types.Header) { *h.ExcessBlobGas = 1 },
		"beacon-root":        func(h *types.Header) { (*h.ParentBeaconRoot)[0] = 1 },
		"missing-requests":   func(h *types.Header) { h.RequestsHash = nil },
		"future-access-list": func(h *types.Header) { h.BlockAccessListHash = new(common.Hash) },
		"future-slot":        func(h *types.Header) { h.SlotNumber = new(uint64) },
	} {
		t.Run(name, func(t *testing.T) {
			bad := types.CopyHeader(candidate)
			mutate(bad)
			f.sign(t, bad)
			if err := f.engine.VerifyHeader(f.chain, bad); err == nil {
				t.Fatal("accepted malformed modern header")
			} else {
				t.Log(err)
			}
		})
	}
	t.Run("proposer-signature", func(t *testing.T) {
		bad := types.CopyHeader(candidate)
		bad.Extra[len(bad.Extra)-65] ^= 1
		if err := f.engine.VerifyHeader(f.chain, bad); err == nil {
			t.Fatal("accepted altered proposer signature")
		} else {
			t.Log(err)
		}
	})
	t.Run("bls-signature", func(t *testing.T) {
		var vote types.VoteAttestation
		if err := rlp.DecodeBytes(candidate.Extra[32:len(candidate.Extra)-65], &vote); err != nil {
			t.Fatal(err)
		}
		vote.AggSignature[0] ^= 1
		bad := types.CopyHeader(candidate)
		bad.Extra = f.extra(false, &vote)
		f.sign(t, bad)
		if err := f.engine.VerifyHeader(f.chain, bad); err == nil {
			t.Fatal("accepted altered BLS signature")
		} else {
			t.Log(err)
		}
	})
	t.Run("well-formed-wrong-bls-message", func(t *testing.T) {
		var vote types.VoteAttestation
		if err := rlp.DecodeBytes(candidate.Extra[32:len(candidate.Extra)-65], &vote); err != nil {
			t.Fatal(err)
		}
		wrongMessage := vote.Data.Hash()
		wrongMessage[0] ^= 1
		var signatures []bls.Signature
		for _, key := range f.votes {
			signatures = append(signatures, key.Sign(wrongMessage[:]))
		}
		copy(vote.AggSignature[:], bls.AggregateSignatures(signatures).Marshal())
		bad := types.CopyHeader(candidate)
		bad.Extra = f.extra(false, &vote)
		f.sign(t, bad)
		if err := f.engine.VerifyHeader(f.chain, bad); err == nil || err.Error() != "invalid attestation, signature verify failed" {
			t.Fatalf("expected BLS verification failure, got %v", err)
		} else {
			t.Log(err)
		}
	})
	t.Run("fork-order", func(t *testing.T) {
		broken := modernWireConfig(t)
		broken.PragueTime = nil
		if err := broken.CheckConfigForkOrder(); err == nil {
			t.Fatal("missing Prague accepted")
		} else {
			t.Log(err)
		}
		broken = modernWireConfig(t)
		broken.BlobScheduleConfig.Prague = nil
		if err := broken.CheckConfigForkOrder(); err == nil {
			t.Fatal("missing Prague blob schedule accepted")
		} else {
			t.Log(err)
		}
	})
	s := snapshot(t, f.engine, f.chain, f.chain.head)
	if s.Attestation == nil || s.Attestation.TargetNumber != 11 {
		t.Fatal("missing verified modern attestation")
	}
	t.Logf("fork order valid; decoded and verified 12 successors; genesis=%s head12=%s; epoch=%d interval=%d turn=%d", f.chain.GenesisHeader().Hash(), f.chain.head.Hash(), s.EpochLength, s.BlockInterval, s.TurnLength)
}

func TestHeaderSemanticBigIntNilAndZero(t *testing.T) {
	a := &types.Header{Number: new(big.Int)}
	b := &types.Header{Number: new(big.Int).SetUint64(1)}
	b.Number.Sub(b.Number, big.NewInt(1))
	if field := headerSemanticDifference(a, b); field != "" {
		t.Fatalf("equal numeric zero differs: %s", field)
	}
	b.Number = nil
	if field := headerSemanticDifference(a, b); field != "Number" {
		t.Fatalf("lost nil distinction: %s", field)
	}
}

// This redoes the checkpoint experiment with a new genesis, all new hash links,
// new proposer/vote signatures, and new snapshots. It does not reinterpret any
// legacy proof digest. Every retained header is a canonical official RLP decode.
func TestModernWireCheckpointRoundTrip(t *testing.T) {
	f := makeModernWireFixture(t)
	f.advance(t, 2048)
	support := snapshot(t, f.engine, f.chain, f.chain.head)
	f.advance(t, 3072)
	anchor := snapshot(t, f.engine, f.chain, f.chain.head)
	candidate := canonicalWireHeader(t, f.next(t, 1))
	if err := f.engine.VerifyHeader(f.chain, candidate); err != nil {
		t.Fatalf("full replay successor: %v", err)
	}
	f.chain.add(candidate)
	expected := snapshot(t, f.engine, f.chain, candidate)
	if support.EpochLength != 1000 || anchor.EpochLength != 1000 || anchor.BlockInterval != 450 || anchor.TurnLength != 32 {
		t.Fatal("unexpected modern checkpoint profile")
	}
	foundSentinel := false
	for n, addr := range support.Recents {
		if n > support.Number && addr == (common.Address{}) {
			foundSentinel = true
		}
	}
	if !foundSentinel {
		t.Fatal("missing Bohr epoch sentinel")
	}

	// Persist and reopen canonical wire bytes and the pinned private snapshot
	// JSON. These are local synthetic test artifacts, NOT an authenticated bundle
	// format, importer, or permission to trust untrusted snapshot JSON.
	files := map[string][]byte{}
	var err error
	files["config.json"], err = json.MarshalIndent(f.chain.cfg, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	var all []*types.Header
	for n := uint64(0); n <= 3073; n++ {
		all = append(all, f.chain.byNumber[n])
	}
	files["headers.rlp"], err = rlp.EncodeToBytes(all)
	if err != nil {
		t.Fatal(err)
	}
	files["snapshot-2048.json"] = encoded(t, support)
	files["snapshot-3072.json"] = encoded(t, anchor)
	files["snapshot-3073.json"] = encoded(t, expected)
	dir := t.TempDir()
	for name, b := range files {
		if err := os.WriteFile(filepath.Join(dir, name), b, 0600); err != nil {
			t.Fatal(err)
		}
	}
	read := func(name string) []byte {
		b, err := os.ReadFile(filepath.Join(dir, name))
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(b, files[name]) {
			t.Fatalf("disk bytes differ for %s", name)
		}
		return b
	}
	var cfg params.ChainConfig
	if err := json.Unmarshal(read("config.json"), &cfg); err != nil {
		t.Fatal(err)
	}
	if err := cfg.CheckConfigForkOrder(); err != nil {
		t.Fatal(err)
	}
	var decoded []*types.Header
	if err := rlp.DecodeBytes(read("headers.rlp"), &decoded); err != nil {
		t.Fatal(err)
	}
	if len(decoded) != len(all) {
		t.Fatal("header count changed on disk")
	}
	reencoded, err := rlp.EncodeToBytes(decoded)
	if err != nil || !bytes.Equal(reencoded, files["headers.rlp"]) {
		t.Fatalf("noncanonical header list: %v", err)
	}
	restored := &chain{cfg: &cfg, byNumber: map[uint64]*types.Header{}, byHash: map[common.Hash]*types.Header{}}
	for i, h := range decoded {
		if field := headerSemanticDifference(all[i], h); field != "" || all[i].Hash() != h.Hash() {
			t.Fatalf("disk header %d differs: %s", i, field)
		}
		if h.Number.Uint64() != uint64(i) || (i > 0 && h.ParentHash != decoded[i-1].Hash()) {
			t.Fatalf("disk header %d unlinked", i)
		}
		restored.add(h)
	}
	load := func(name string) *parlia.Snapshot {
		var s parlia.Snapshot
		if err := json.Unmarshal(read(name), &s); err != nil {
			t.Fatal(err)
		}
		return &s
	}
	loadedSupport, loadedAnchor, loadedExpected := load("snapshot-2048.json"), load("snapshot-3072.json"), load("snapshot-3073.json")

	// Anchor-only cannot reconstruct the attestation target-parent snapshot.
	pruned := restored.slice(3069, 3072)
	anchorOnly, _ := fresh(t, pruned, loadedAnchor)
	if err := anchorOnly.VerifyHeader(pruned, decoded[3073]); err == nil {
		t.Fatal("anchor-only unexpectedly sufficient")
	} else {
		t.Logf("anchor-only rejection: %v", err)
	}
	bounded := restored.slice(2000, 3072)
	derivedEngine, _ := fresh(t, bounded, loadedSupport)
	derived := snapshot(t, derivedEngine, bounded, bounded.head)
	if !bytes.Equal(encoded(t, derived), encoded(t, loadedAnchor)) {
		t.Fatal("wire support replay differs from checkpoint")
	}
	resumed, _ := fresh(t, bounded, loadedSupport, loadedAnchor)
	if err := resumed.VerifyHeader(bounded, decoded[3073]); err != nil {
		t.Fatalf("wire bounded successor: %v", err)
	}
	bounded.add(decoded[3073])
	got := snapshot(t, resumed, bounded, bounded.head)
	if !bytes.Equal(encoded(t, got), encoded(t, loadedExpected)) {
		t.Fatal("wire resumed snapshot differs from full replay")
	}
	if resumed.VotePool != nil {
		t.Fatal("VotePool must be nil")
	}
	justified, jhash, err := resumed.GetJustifiedNumberAndHash(bounded, []*types.Header{bounded.head})
	if err != nil || justified != 3072 || jhash != decoded[3072].Hash() {
		t.Fatalf("unexpected justified result: %d %s %v", justified, jhash, err)
	}
	finalized := resumed.GetFinalizedHeader(bounded, bounded.head)
	if finalized == nil || finalized.Number.Uint64() != 3071 || finalized.Hash() != decoded[3071].Hash() {
		t.Fatal("unexpected synthetic finality")
	}

	// A second fresh engine and DB re-import the disk seed records and recover
	// 3073. No engine/cache pointer or nonaligned 3073 DB record is reused.
	if err := resumed.Close(); err != nil {
		t.Fatal(err)
	}
	restarted, _ := fresh(t, bounded, load("snapshot-2048.json"), load("snapshot-3072.json"))
	if !bytes.Equal(encoded(t, snapshot(t, restarted, bounded, bounded.head)), encoded(t, loadedExpected)) {
		t.Fatal("fresh-engine restart snapshot differs")
	}
	broken := restored.slice(2001, 3072)
	missingEpoch, _ := fresh(t, broken, loadedSupport, loadedAnchor)
	if err := missingEpoch.VerifyHeader(broken, decoded[3073]); err == nil {
		t.Fatal("missing epoch ancestor accepted")
	} else {
		t.Logf("missing epoch rejection: %v", err)
	}

	type artifactManifest struct {
		Profile     string            `json:"profile"`
		Scope       string            `json:"scope"`
		HeaderCount int               `json:"header_count"`
		Genesis     common.Hash       `json:"genesis"`
		Support     common.Hash       `json:"support_2048"`
		Anchor      common.Hash       `json:"anchor_3072"`
		Successor   common.Hash       `json:"successor_3073"`
		SHA256      map[string]string `json:"sha256"`
	}
	manifest := artifactManifest{Profile: "synthetic-modern-wire-v1-bsc-1.8.2-through-fermi", Scope: "Offline test data only. Header-only, no execution, state/system-contract validation, real BSC history, trusted bundle importer, or production readiness. All hashes/snapshots are new and incompatible with the legacy fixture.", HeaderCount: len(decoded), Genesis: decoded[0].Hash(), Support: decoded[2048].Hash(), Anchor: decoded[3072].Hash(), Successor: decoded[3073].Hash(), SHA256: map[string]string{}}
	for name, b := range files {
		sum := sha256.Sum256(b)
		manifest.SHA256[name] = common.Bytes2Hex(sum[:])
	}
	files["manifest.json"], err = json.MarshalIndent(manifest, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	// Optional explicit research export. Refuse to overwrite an existing path.
	if out := os.Getenv("BSC_MODERN_WIRE_OUTPUT_DIR"); out != "" {
		if err := os.Mkdir(out, 0700); err != nil {
			t.Fatal(err)
		}
		for name, b := range files {
			if err := os.WriteFile(filepath.Join(out, name), b, 0600); err != nil {
				t.Fatal(err)
			}
		}
		t.Logf("exported synthetic artifacts: %s", out)
	}
	t.Logf("canonical RLP headers=%d bytes=%d sha256=%s", len(decoded), len(files["headers.rlp"]), manifest.SHA256["headers.rlp"])
	t.Logf("new hashes: genesis=%s support2048=%s anchor3072=%s next3073=%s", manifest.Genesis, manifest.Support, manifest.Anchor, manifest.Successor)
	t.Logf("full replay, wire reload, S->C replay, successor verification and fresh-engine restart agree; bounded headers=%d; synthetic justified=3072 finalized=3071", len(bounded.byHash))
}
