// SPDX-License-Identifier: GPL-3.0-or-later
//go:build blscacherace

package checkpointproof

import (
	"sync"
	"testing"

	"github.com/prysmaticlabs/prysm/v5/crypto/bls"
)

// Diagnostic only: the pinned upstream cache has a known concurrency race.
// Run explicitly with -race -tags=blscacherace; see docs/BLS_CACHE_KNOWN_ISSUE.md.
func TestBLSPublicKeyConcurrentFirstUse(t *testing.T) {
	for n := 1; n <= 25; n++ {
		b := make([]byte, 32)
		b[30] = 2
		b[31] = byte(n)
		k, err := bls.SecretKeyFromBytes(b)
		if err != nil {
			t.Fatal(err)
		}
		raw := k.PublicKey().Marshal()
		start := make(chan struct{})
		var wg sync.WaitGroup
		for i := 0; i < 64; i++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				<-start
				for j := 0; j < 3; j++ {
					if _, err := bls.PublicKeyFromBytes(raw); err != nil {
						t.Error(err)
					}
				}
			}()
		}
		close(start)
		wg.Wait()
	}
}
