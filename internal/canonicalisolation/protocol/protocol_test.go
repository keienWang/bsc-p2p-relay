//go:build canonicalisolation

// SPDX-License-Identifier: GPL-3.0-or-later
package protocol

import (
	"strings"
	"testing"
)

func TestStrictJSON(t *testing.T) {
	type nested struct {
		Value int `json:"value"`
	}
	type envelope struct {
		Object nested `json:"object"`
		Array  []int  `json:"array"`
	}
	for name, raw := range map[string]string{
		"duplicate":         `{"object":{"value":1,"value":2}}`,
		"escaped_duplicate": `{"object":{"value":1,"\u0076alue":2}}`,
		"unknown":           `{"object":{"unknown":1}}`,
		"trailing":          `{} {}`,
		"deep":              strings.Repeat("[", 18) + "0" + strings.Repeat("]", 18),
		"truncated":         `{"object":`,
	} {
		t.Run(name, func(t *testing.T) {
			var out envelope
			if StrictJSON([]byte(raw), &out) == nil {
				t.Fatal("accepted invalid JSON")
			}
		})
	}
	var out envelope
	if err := StrictJSON([]byte(`{"object":{"value":2},"array":[1,2]}`), &out); err != nil || out.Object.Value != 2 {
		t.Fatalf("valid JSON: %+v %v", out, err)
	}
}

// This is a rejection smoke check. Empty companion fields mean it does not
// isolate each size boundary; TestAuthenticatedBundleLimits does that using
// the complete independently pinned fixture in the worker test package.
func TestBundleBoundsBeforeAuthentication(t *testing.T) {
	for _, bundle := range []Bundle{{}, {Config: make([]byte, MaxRecord+1)}, {Headers: make([]byte, HeaderBytes+1)}} {
		if bundle.Authenticate() == nil {
			t.Fatal("invalid bundle authenticated")
		}
	}
	if Digest([]byte("abc")) != "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad" {
		t.Fatal("digest changed")
	}
}
