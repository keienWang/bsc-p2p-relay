//go:build canonicalisolation

// SPDX-License-Identifier: GPL-3.0-or-later
package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
	"runtime"

	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/crypto"
	"github.com/ethereum/go-ethereum/rlp"
	"github.com/keienWang/bsc-p2p-relay/internal/canonicalisolation/protocol"
	"github.com/prysmaticlabs/prysm/v5/crypto/bls"
)

// This generator is another fresh process, not code linked into the supervisor.
// It uses only the public scalars already documented by the synthetic fixture.
func candidateMain() {
	raw, err := io.ReadAll(io.LimitReader(os.Stdin, protocol.MaxInput+1))
	if err != nil || len(raw) > protocol.MaxInput || runtime.GOMAXPROCS(0) != 2 {
		os.Exit(2)
	}
	var bundle protocol.Bundle
	if protocol.StrictJSON(raw, &bundle) != nil {
		os.Exit(2)
	}
	candidates, err := makeCandidates(bundle)
	if err != nil {
		os.Exit(2)
	}
	encoded, err := json.Marshal(candidates)
	if err != nil || len(encoded)+1 > protocol.MaxOutput {
		os.Exit(2)
	}
	if _, err = os.Stdout.Write(append(encoded, '\n')); err != nil {
		os.Exit(2)
	}
}

func makeCandidates(bundle protocol.Bundle) (protocol.Candidates, error) {
	cfg, headers, _, err := decodeBundle(bundle)
	if err != nil {
		return protocol.Candidates{}, err
	}
	h := headers[protocol.CandidateNumber]
	var vote types.VoteAttestation
	if err := rlp.DecodeBytes(h.Extra[32:len(h.Extra)-65], &vote); err != nil {
		return protocol.Candidates{}, err
	}
	correct := vote.Data.Hash()
	wrong := correct
	wrong[0] ^= 1
	var pubs []bls.PublicKey
	var signatures []bls.Signature
	for scalar := byte(11); scalar <= 13; scalar++ {
		raw := make([]byte, 32)
		raw[31] = scalar
		key, err := bls.SecretKeyFromBytes(raw)
		if err != nil {
			return protocol.Candidates{}, err
		}
		pubs = append(pubs, key.PublicKey())
		signatures = append(signatures, key.Sign(wrong[:]))
	}
	original, err := bls.SignatureFromBytes(vote.AggSignature[:])
	if err != nil || !original.FastAggregateVerify(pubs, correct) {
		return protocol.Candidates{}, errors.New("original signature control failed")
	}
	copy(vote.AggSignature[:], bls.AggregateSignatures(signatures).Marshal())
	altered, err := bls.SignatureFromBytes(vote.AggSignature[:])
	if err != nil || !altered.FastAggregateVerify(pubs, wrong) || altered.FastAggregateVerify(pubs, correct) {
		return protocol.Candidates{}, errors.New("wrong-message signature control failed")
	}
	payload, err := rlp.EncodeToBytes(&vote)
	if err != nil {
		return protocol.Candidates{}, err
	}
	bad := types.CopyHeader(h)
	bad.Extra = append(bytes.Clone(h.Extra[:32]), payload...)
	bad.Extra = append(bad.Extra, make([]byte, 65)...)
	signed := false
	for scalar := byte(1); scalar <= 3; scalar++ {
		raw := make([]byte, 32)
		raw[31] = scalar
		key, err := crypto.ToECDSA(raw)
		if err != nil {
			return protocol.Candidates{}, err
		}
		if crypto.PubkeyToAddress(key.PublicKey) != bad.Coinbase {
			continue
		}
		hash := types.SealHash(bad, cfg.ChainID)
		signature, err := crypto.Sign(hash[:], key)
		if err != nil {
			return protocol.Candidates{}, err
		}
		copy(bad.Extra[len(bad.Extra)-65:], signature)
		recovered, err := crypto.SigToPub(hash[:], signature)
		if err != nil || crypto.PubkeyToAddress(*recovered) != bad.Coinbase {
			return protocol.Candidates{}, errors.New("proposer recovery control failed")
		}
		signed = true
		break
	}
	if !signed {
		return protocol.Candidates{}, errors.New("no public fixture proposer")
	}
	goodWire, err := rlp.EncodeToBytes(h)
	if err != nil {
		return protocol.Candidates{}, err
	}
	badWire, err := rlp.EncodeToBytes(bad)
	if err != nil {
		return protocol.Candidates{}, err
	}
	if _, err = decodeHeader(goodWire); err != nil {
		return protocol.Candidates{}, err
	}
	if _, err = decodeHeader(badWire); err != nil {
		return protocol.Candidates{}, err
	}
	return protocol.Candidates{Good: goodWire, WrongMessage: badWire, Controls: true}, nil
}
