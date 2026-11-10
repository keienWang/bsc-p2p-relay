//go:build canonicalisolation

// SPDX-License-Identifier: GPL-3.0-or-later
// Package protocol is an opt-in, offline test protocol. It has no BSC/BLS imports
// and is not a production importer, authentication format or service API.
package protocol

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
)

const (
	FixtureID        = "synthetic-modern-wire-v1-bsc-1.8.2-through-fermi"
	MaxInput         = 4 << 20 // JSON/base64 envelope including the authenticated RLP list.
	MaxOutput        = 4096
	MaxRecord        = 8192
	HeaderBytes      = 2714112
	HeaderCount      = 3074
	MaxRetained      = 1073 // [2000,3072], plus separately admitted genesis.
	MaxSnapshots     = 2
	MaxValidators    = 3
	CandidateNumber  = 3073
	GenesisHash      = "0xfa793de59f8a91705c57e00bc6df547c4ee7c32ea86ab5f2e835b077cb8002b9"
	CandidateHash    = "0x7070d0125212a3c961b2ef2eeae48a25aebaa7909e13d5815356e4353b14da0c"
	ExpectedSnapshot = "27fd094f93d0ffb4126766de6dc96cf8070f7a6bb0825f00f507187282352317"
)

// Pins are compiled from the independently reproduced modern-wire fixture, not
// supplied by a peer/request. The inventory manifest.json is not a trust root.
var Pins = map[string]string{
	"config.json":        "d98c4d7d24c9fcc47652eb74dfc07fc4febaf3ab1b0ca5ec0f15b3dec93a859e",
	"headers.rlp":        "08d59559eb1dce022f15f345cd858150ec5ce1770b36155cf5604b1291fdea5a",
	"snapshot-2048.json": "eda6edbdc21f96f8373f419258f4e1600240c418d2e7640b0a3d76a1ae3420a4",
	"snapshot-3072.json": "4874f8aba8e3dbe4f8e04b4133468b36348c09dd049c2c7ffb152f859a99180b",
}

type Bundle struct {
	Config  []byte `json:"config"`
	Headers []byte `json:"headers"`
	Support []byte `json:"support"`
	Anchor  []byte `json:"anchor"`
}

type Request struct {
	FixtureID string   `json:"fixture_id"`
	Bundle    Bundle   `json:"bundle"`
	Retained  []uint64 `json:"retained"`
	Snapshots []uint64 `json:"snapshots"`
	Candidate []byte   `json:"candidate"` // canonical official Header RLP; not authenticated.
	Replay    bool     `json:"replay"`    // full official header verification, never snapshot-only replay.
}

type Response struct {
	Accepted                     bool   `json:"accepted"`
	Stage                        string `json:"stage"`
	Error                        string `json:"error,omitempty"`
	FixtureID                    string `json:"fixture_id"`
	RequestDigest                string `json:"request_digest"`
	CandidateDigest              string `json:"candidate_digest,omitempty"`
	CandidateHash                string `json:"candidate_hash,omitempty"`
	SnapshotDigest               string `json:"snapshot_digest,omitempty"`
	Justified                    uint64 `json:"justified,omitempty"`
	Finalized                    uint64 `json:"finalized,omitempty"`
	VerifiedHeaders              int    `json:"verified_headers"`
	Reads                        int    `json:"reads"`
	Misses                       int    `json:"misses"`
	CandidateVisibleBeforeVerify bool   `json:"candidate_visible_before_verify"`
}

type Candidates struct {
	Good         []byte `json:"good"`
	WrongMessage []byte `json:"wrong_message"`
	Controls     bool   `json:"controls"`
}

func Digest(b []byte) string { h := sha256.Sum256(b); return hex.EncodeToString(h[:]) }

// StrictJSON rejects duplicate object keys (including nested keys), excessive
// nesting, unknown struct fields, and multiple values. Snapshot validation also
// requires exact marshal round trips, catching aliases, omitted required fields,
// shortened fixed arrays, case-folded field names and integer-key normalization.
func StrictJSON(b []byte, out any) error {
	d := json.NewDecoder(bytes.NewReader(b))
	d.UseNumber()
	var value func(int) error
	value = func(depth int) error {
		if depth > 16 {
			return errors.New("JSON nesting exceeds bound")
		}
		tok, err := d.Token()
		if err != nil {
			return err
		}
		delim, ok := tok.(json.Delim)
		if !ok {
			return nil
		}
		switch delim {
		case '{':
			seen := map[string]bool{}
			for d.More() {
				key, err := d.Token()
				if err != nil {
					return err
				}
				s, ok := key.(string)
				if !ok || seen[s] {
					return errors.New("duplicate or invalid JSON key")
				}
				seen[s] = true
				if err := value(depth + 1); err != nil {
					return err
				}
			}
			end, err := d.Token()
			if err != nil || end != json.Delim('}') {
				return errors.New("invalid JSON object")
			}
		case '[':
			for d.More() {
				if err := value(depth + 1); err != nil {
					return err
				}
			}
			end, err := d.Token()
			if err != nil || end != json.Delim(']') {
				return errors.New("invalid JSON array")
			}
		default:
			return errors.New("unexpected JSON delimiter")
		}
		return nil
	}
	if err := value(0); err != nil {
		return err
	}
	if _, err := d.Token(); err != io.EOF {
		return errors.New("exactly one JSON value required")
	}
	d = json.NewDecoder(bytes.NewReader(b))
	d.DisallowUnknownFields()
	return d.Decode(out)
}

func (b Bundle) Authenticate() error {
	blobs := map[string][]byte{"config.json": b.Config, "headers.rlp": b.Headers, "snapshot-2048.json": b.Support, "snapshot-3072.json": b.Anchor}
	for name, raw := range blobs {
		limit := MaxRecord
		if name == "headers.rlp" {
			limit = HeaderBytes
		}
		if len(raw) == 0 || len(raw) > limit {
			return errors.New("bundle record size exceeds bound")
		}
		if Digest(raw) != Pins[name] {
			return errors.New("context does not match compiled synthetic provenance")
		}
	}
	return nil
}
