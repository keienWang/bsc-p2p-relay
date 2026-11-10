// SPDX-License-Identifier: GPL-3.0-or-later

package firstepoch

import (
	"bytes"
	"crypto/ecdsa"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"math/big"
	"os"
	"sort"
	"strings"
	"sync"
	"testing"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/consensus/parlia"
	"github.com/ethereum/go-ethereum/core"
	"github.com/ethereum/go-ethereum/core/rawdb"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/crypto"
	"github.com/ethereum/go-ethereum/params"
	"github.com/ethereum/go-ethereum/rlp"
)

func encode(t testing.TB, h *types.Header) []byte {
	t.Helper()
	b, err := rlp.EncodeToBytes(h)
	if err != nil {
		t.Fatal(err)
	}
	return b
}
func fixture(t testing.TB, limit int) (*Verifier, []*ecdsa.PrivateKey) {
	t.Helper()
	keys := make([]*ecdsa.PrivateKey, 3)
	for i := range keys {
		b := make([]byte, 32)
		b[31] = byte(i + 1)
		var err error
		keys[i], err = crypto.ToECDSA(b)
		if err != nil {
			t.Fatal(err)
		}
	}
	sort.Slice(keys, func(i, j int) bool {
		return bytes.Compare(crypto.PubkeyToAddress(keys[i].PublicKey).Bytes(), crypto.PubkeyToAddress(keys[j].PublicKey).Bytes()) < 0
	})
	extra := make([]byte, 32)
	for _, k := range keys {
		extra = append(extra, crypto.PubkeyToAddress(k.PublicKey).Bytes()...)
	}
	extra = append(extra, make([]byte, 65)...)
	genesis := &types.Header{Number: big.NewInt(0), Difficulty: big.NewInt(1), GasLimit: 40000000, Time: 1600000000, UncleHash: types.EmptyUncleHash, Extra: extra}
	v, err := newAnchored(params.BSCChainConfig, genesis, limit)
	if err != nil {
		t.Fatal(err)
	}
	return v, keys
}
func signed(t testing.TB, v *Verifier, keys []*ecdsa.PrivateKey) *types.Header {
	t.Helper()
	p := v.Tip()
	n := p.Number.Uint64() + 1
	k := keys[n%uint64(len(keys))]
	h := &types.Header{Number: new(big.Int).SetUint64(n), ParentHash: p.Hash(), Difficulty: big.NewInt(2), GasLimit: p.GasLimit, Time: p.Time + 30, UncleHash: types.EmptyUncleHash, Extra: make([]byte, 97), Coinbase: crypto.PubkeyToAddress(k.PublicKey)}
	resign(t, v, h, k)
	return h
}
func resign(t testing.TB, v *Verifier, h *types.Header, k *ecdsa.PrivateKey) {
	t.Helper()
	db := rawdb.NewMemoryDatabase()
	defer db.Close()
	engine := parlia.New(v.chain.cfg, db, nil, v.chain.GenesisHeader().Hash())
	defer engine.Close()
	sig, err := crypto.Sign(engine.SealHash(h).Bytes(), k)
	if err != nil {
		t.Fatal(err)
	}
	copy(h.Extra[len(h.Extra)-65:], sig)
}
func official(v *Verifier, h *types.Header) error {
	db := rawdb.NewMemoryDatabase()
	defer db.Close()
	engine := parlia.New(v.chain.cfg, db, nil, v.chain.GenesisHeader().Hash())
	defer engine.Close()
	return engine.VerifyHeader(v.chain, h)
}
func TestSignedProgressionAndBounds(t *testing.T) {
	if MaxHeaders >= int(params.FullImmutabilityThreshold) {
		t.Fatal("upstream fallback safety assumption changed")
	}
	v, keys := fixture(t, MaxHeaders)
	for i := 1; i < MaxHeaders; i++ {
		h := signed(t, v, keys)
		// Sample independent differential checks at initial, middle and boundary
		// heights; Append itself still verifies every header with the engine.
		if i <= 3 || i == 100 || i >= MaxHeaders-2 {
			if err := official(v, h); err != nil {
				t.Fatalf("official %d: %v", i, err)
			}
		}
		got, err := v.Append(encode(t, h))
		if err != nil {
			t.Fatalf("append %d: %v", i, err)
		}
		if got.Hash() != h.Hash() {
			t.Fatal("hash")
		}
		got.Extra[0] ^= 1
		if v.Tip().Hash() != h.Hash() {
			t.Fatal("output aliases retained state")
		}
	}

	// At the maximum supported height, fresh-engine reconstruction still writes
	// exactly one bounded genesis snapshot and no periodic/checkpoint state.
	db := rawdb.NewMemoryDatabase()
	defer db.Close()
	engine := parlia.New(v.chain.cfg, db, nil, v.chain.GenesisHeader().Hash())
	defer engine.Close()
	if err := engine.VerifyHeader(v.chain, v.Tip()); err != nil {
		t.Fatal(err)
	}
	it := db.NewIterator(nil, nil)
	count := 0
	expectedKey := append([]byte("parlia-"), v.chain.GenesisHeader().Hash().Bytes()...)
	for it.Next() {
		count++
		if !bytes.Equal(it.Key(), expectedKey) || len(it.Value()) > 64*1024 {
			t.Fatal("unexpected snapshot DB growth")
		}
	}
	if err := it.Error(); err != nil {
		t.Fatal(err)
	}
	it.Release()
	if count != 1 {
		t.Fatalf("snapshot record count %d", count)
	}
	before := v.Tip().Hash()
	if _, err := v.Append(encode(t, signed(t, v, keys))); !errors.Is(err, ErrLimit) {
		t.Fatal(err)
	}
	if v.Tip().Hash() != before {
		t.Fatal("limit advanced tip")
	}
}
func TestInvalidSignedHeaders(t *testing.T) {
	for _, name := range []string{"seal", "unauthorized", "difficulty", "gas", "timestamp", "wrong_chain", "recent_signer", "wrong_parent", "wrong_number", "time_fork", "amsterdam", "epoch"} {
		t.Run(name, func(t *testing.T) {
			v, keys := fixture(t, MaxHeaders)
			h := signed(t, v, keys)
			switch name {
			case "seal":
				h.Extra[96] ^= 1
			case "unauthorized":
				h.Coinbase = common.Address{1}
			case "difficulty":
				h.Difficulty = big.NewInt(1)
				resign(t, v, h, keys[1])
			case "gas":
				h.GasUsed = h.GasLimit + 1
				resign(t, v, h, keys[1])
			case "timestamp":
				h.Time = v.Tip().Time
				resign(t, v, h, keys[1])
			case "wrong_chain":
				v.chain.cfg.ChainID = big.NewInt(97)
			case "recent_signer":
				if _, err := v.Append(encode(t, h)); err != nil {
					t.Fatal(err)
				}
				h = signed(t, v, keys)
				h.Coinbase = crypto.PubkeyToAddress(keys[1].PublicKey)
				h.Difficulty = big.NewInt(1)
				resign(t, v, h, keys[1])
			case "wrong_parent":
				h.ParentHash = common.Hash{1}
			case "wrong_number":
				h.Number = big.NewInt(3)
			case "time_fork":
				h.Time = *v.chain.cfg.ShanghaiTime
			case "amsterdam":
				h.BlockAccessListHash = new(common.Hash)
				h.SlotNumber = new(uint64)
			case "epoch":
				h.Number = big.NewInt(200)
			}
			before := v.Tip().Hash()
			if name != "epoch" && name != "time_fork" && official(v, h) == nil {
				t.Fatal("official accepted invalid header")
			}
			if got, err := v.Append(encode(t, h)); err == nil || got != nil {
				t.Fatal("accepted invalid header")
			}
			if v.Tip().Hash() != before {
				t.Fatal("invalid header advanced tip")
			}
			// A failed candidate must not poison the next legitimate append.
			if name == "wrong_chain" {
				v.chain.cfg.ChainID = big.NewInt(56)
			}
			if _, err := v.Append(encode(t, signed(t, v, keys))); err != nil {
				t.Fatal(err)
			}
		})
	}
}
func TestInputLimitsAndIsolation(t *testing.T) {
	v, keys := fixture(t, 2)
	h := signed(t, v, keys)
	good := encode(t, h)
	for _, b := range [][]byte{nil, {0xc0}, append(append([]byte{}, good...), 0), make([]byte, MaxHeaderBytes+1)} {
		if got, err := v.Append(b); err == nil || got != nil {
			t.Fatal("accepted bad encoding")
		}
	}
	if _, err := newAnchored(v.chain.cfg, v.Tip(), MaxHeaders+1); !errors.Is(err, ErrLimit) {
		t.Fatal(err)
	}
	returned := v.Tip()
	returned.Extra[0] ^= 1
	if returned.Hash() == v.Tip().Hash() {
		t.Fatal("tip aliases state")
	}
	if _, err := v.Append(good); err != nil {
		t.Fatal(err)
	}
	if _, err := v.Append(good); !errors.Is(err, ErrLimit) {
		t.Fatal(err)
	}
	var zero Verifier
	if _, err := zero.Append(good); !errors.Is(err, ErrAnchor) {
		t.Fatal(err)
	}
}
func TestOfficialGenesisAnchors(t *testing.T) {
	for _, tc := range []struct {
		id   uint64
		file string
		hash common.Hash
	}{{56, "testdata/mainnet-genesis.rlp", params.BSCGenesisHash}, {97, "testdata/chapel-genesis.rlp", params.ChapelGenesisHash}} {
		b, err := os.ReadFile(tc.file)
		if err != nil {
			t.Fatal(err)
		}
		v, err := New(tc.id, b, MaxHeaders)
		if err != nil {
			t.Fatal(err)
		}
		if v.Tip().Hash() != tc.hash {
			t.Fatal("wrong official genesis")
		}
		if _, err := New(153-tc.id, b, MaxHeaders); !errors.Is(err, ErrAnchor) {
			t.Fatal(err)
		}
		b[len(b)-1] ^= 1
		if _, err := New(tc.id, b, MaxHeaders); err == nil {
			t.Fatal("mutated genesis accepted")
		}
	}
	if _, err := New(1, nil, MaxHeaders); !errors.Is(err, ErrUnsupported) {
		t.Fatal(err)
	}
	v, _ := fixture(t, 2)
	if _, err := New(56, encode(t, v.Tip()), 2); !errors.Is(err, ErrAnchor) {
		t.Fatal("synthetic anchor accepted publicly")
	}
}
func FuzzAppend(f *testing.F) {
	seed, keys := fixture(f, 2)
	f.Add(encode(f, signed(f, seed, keys)))
	f.Add([]byte{0xc0})
	f.Add([]byte{})
	f.Fuzz(func(t *testing.T, b []byte) {
		v, _ := fixture(t, 2)
		before := v.Tip().Hash()
		got, err := v.Append(b)
		if err != nil && (got != nil || v.Tip().Hash() != before) {
			t.Fatal("failure changed state")
		}
	})
}

// Check provenance without network access: the fixture must reproduce byte for
// byte from the exact official genesis allocation and header specification.
func TestFixtureProvenance(t *testing.T) {
	for name, g := range map[string]*core.Genesis{"mainnet": core.DefaultBSCGenesisBlock(), "chapel": core.DefaultChapelGenesisBlock()} {
		got, err := os.ReadFile("testdata/" + name + "-genesis.rlp")
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(got, encode(t, g.ToBlock().Header())) {
			t.Fatal("fixture differs from official genesis:", name)
		}
	}
}
func TestConcurrentAppend(t *testing.T) {
	v, keys := fixture(t, MaxHeaders)
	b := encode(t, signed(t, v, keys))
	results := make(chan error, 2)
	var wg sync.WaitGroup
	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); _, err := v.Append(b); results <- err }()
	}
	wg.Wait()
	close(results)
	accepted := 0
	for err := range results {
		if err == nil {
			accepted++
		} else if !errors.Is(err, ErrAncestry) {
			t.Fatal(err)
		}
	}
	if accepted != 1 || v.Tip().Number.Uint64() != 1 {
		t.Fatal("non-atomic append")
	}
}

func TestGenuineMainnetBlockOne(t *testing.T) {
	genesis, err := os.ReadFile("testdata/mainnet-genesis.rlp")
	if err != nil {
		t.Fatal(err)
	}
	v, err := New(56, genesis, MaxHeaders)
	if err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile("testdata/mainnet-block1.json")
	if err != nil {
		t.Fatal(err)
	}
	var h types.Header
	if err = json.Unmarshal(data, &h); err != nil {
		t.Fatal(err)
	}
	expected := common.HexToHash("0x04055304e432294a65ff31069c4d3092ff8b58f009cdb50eba5351e0332ad0f6")
	if h.Hash() != expected {
		t.Fatalf("real fixture hash mismatch: %s", h.Hash())
	}
	if err := official(v, &h); err != nil {
		t.Fatal(err)
	}
	got, err := v.Append(encode(t, &h))
	if err != nil {
		t.Fatal(err)
	}
	if got.Hash() != expected {
		t.Fatal("real header changed")
	}
	// Mutating a seal-bound commitment preserves neither authentication nor tip.
	other, err := New(56, genesis, MaxHeaders)
	if err != nil {
		t.Fatal(err)
	}
	h.Root[0] ^= 1
	if _, err := other.Append(encode(t, &h)); err == nil {
		t.Fatal("mutated genuine header accepted")
	}
}

// Freeze the exact official configs as well as checking the assumptions behind
// the narrow guards. A dependency/config change requires explicit review.
func TestPinnedConfigurationScope(t *testing.T) {
	for _, tc := range []struct {
		cfg         *params.ChainConfig
		fingerprint string
	}{
		{params.BSCChainConfig, "5ff26c329c9dc95ba02ce5f80ea6085d2d24094b1294304ef9dbb54e3f4d3ddf"},
		{params.ChapelChainConfig, "cfd3405d41cc435f385305451c0ae51c5ac3d2a062b78904687f2c0786264d7e"},
	} {
		b, err := json.Marshal(tc.cfg)
		if err != nil {
			t.Fatal(err)
		}
		if fmt.Sprintf("%x", sha256.Sum256(b)) != tc.fingerprint {
			t.Fatal("pinned fork configuration changed; review scope before updating")
		}
		if tc.cfg.ShanghaiTime == nil {
			t.Fatal("missing first timestamp fork")
		}
		var fields map[string]json.RawMessage
		if err := json.Unmarshal(b, &fields); err != nil {
			t.Fatal(err)
		}
		for name, value := range fields {
			if !strings.HasSuffix(name, "Block") && !strings.HasSuffix(name, "Time") {
				continue
			}
			if string(value) == "null" {
				continue
			}
			var n uint64
			if err := json.Unmarshal(value, &n); err != nil {
				t.Fatal(err)
			}
			if strings.HasSuffix(name, "Block") && n > 0 && n < MaxHeaders {
				t.Fatalf("fork %s within supported epoch", name)
			}
			if strings.HasSuffix(name, "Time") && n < *tc.cfg.ShanghaiTime {
				t.Fatalf("timestamp fork %s before guard", name)
			}
		}
	}
}
