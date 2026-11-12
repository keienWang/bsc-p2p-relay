//go:build noexecstack && linux && amd64 && cgo

// SPDX-License-Identifier: GPL-3.0-or-later
package nativecompat

import (
	"bytes"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"testing"

	"github.com/herumi/bls-eth-go-binary/bls"
)

// Public fields only, from pinned upstream verify.txt records 1 and 5.
// See docs/NOEXECSTACK_CANDIDATE.md. No key creation, signing or network use.
//
//go:embed testdata/herumi_public_vectors.json
var publicVectors []byte

func publicHex(t *testing.T, text string) []byte {
	t.Helper()
	value, err := hex.DecodeString(text)
	if err != nil {
		t.Fatal(err)
	}
	return value
}

func TestHerumiPublicVectors(t *testing.T) {
	// Init and mode are global and not thread-safe. This fixture is deliberately
	// sequential, in a separate test process that does not import Prysm.
	if err := bls.Init(bls.BLS12_381); err != nil {
		t.Fatal(err)
	}
	if err := bls.SetETHmode(bls.EthModeDraft07); err != nil {
		t.Fatal(err)
	}
	bls.VerifyPublicKeyOrder(true)
	bls.VerifySignatureOrder(true)
	var vectors []map[string]string
	if err := json.Unmarshal(publicVectors, &vectors); err != nil {
		t.Fatal(err)
	}
	if len(vectors) != 2 {
		t.Fatal("expected two public vectors")
	}
	pubs := make([]bls.PublicKey, 2)
	sigs := make([]bls.Sign, 2)
	message := publicHex(t, vectors[0]["msg"])
	for i, vector := range vectors {
		pk := publicHex(t, vector["pub"])
		sig := publicHex(t, vector["sig"])
		if len(pk) != 48 || len(sig) != 96 || len(message) != 32 || vector["out"] != "true" || !bytes.Equal(message, publicHex(t, vector["msg"])) {
			t.Fatal("unexpected public vector shape")
		}
		if err := pubs[i].Deserialize(pk); err != nil {
			t.Fatal(err)
		}
		if err := sigs[i].Deserialize(sig); err != nil {
			t.Fatal(err)
		}
		if pubs[i].IsZero() || sigs[i].IsZero() || !pubs[i].IsValidOrder() || !sigs[i].IsValidOrder() {
			t.Fatal("invalid public point")
		}
		if !bytes.Equal(pubs[i].Serialize(), pk) || !bytes.Equal(sigs[i].Serialize(), sig) {
			t.Fatal("public point round trip changed bytes")
		}
		if !sigs[i].VerifyByte(&pubs[i], message) {
			t.Fatal("published valid signature rejected")
		}
	}
	if pubs[0].IsEqual(&pubs[1]) || sigs[0].VerifyByte(&pubs[1], message) {
		t.Fatal("different public key accepted")
	}
	var aggregate bls.Sign
	aggregate.Aggregate(sigs)
	combinedKey := pubs[0]
	combinedKey.Add(&pubs[1])
	if !aggregate.FastAggregateVerify(pubs, message) || !aggregate.VerifyByte(&combinedKey, message) {
		t.Fatal("public signature aggregate rejected")
	}
	var roundTrip bls.Sign
	if err := roundTrip.Deserialize(aggregate.Serialize()); err != nil {
		t.Fatal(err)
	}
	if !roundTrip.IsEqual(&aggregate) || !roundTrip.FastAggregateVerify(pubs, message) {
		t.Fatal("aggregate round trip failed")
	}
	wrongMessage := append([]byte(nil), message...)
	wrongMessage[0] ^= 1
	if sigs[0].VerifyByte(&pubs[0], wrongMessage) || aggregate.FastAggregateVerify(pubs, wrongMessage) {
		t.Fatal("changed message accepted")
	}
	var badKey bls.PublicKey
	var badSignature bls.Sign
	if badKey.Deserialize([]byte{1}) == nil || badSignature.Deserialize([]byte{1}) == nil {
		t.Fatal("truncated public point accepted")
	}
	t.Log("Herumi public parse/serialize/order/aggregate/verify paths passed; no signing or secret keys")
}
