// SPDX-License-Identifier: GPL-3.0-or-later
package relay

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/ethereum/go-ethereum/core/types"
)

func readObservationCacheFixture(t *testing.T, client *http.Client, url string, status int, dst any) {
	t.Helper()
	response, err := client.Get(url)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != status || response.Header.Get("Cache-Control") != "no-store" {
		t.Fatalf("GET %s: status=%d cache=%q", url, response.StatusCode, response.Header.Get("Cache-Control"))
	}
	if err := json.NewDecoder(response.Body).Decode(dst); err != nil {
		t.Fatal(err)
	}
}

func TestObservationHTTPSeparatesStatsFromCacheMaintenance(t *testing.T) {
	for _, observeOnly := range []bool{false, true} {
		for _, check := range []struct {
			path   string
			status int
		}{{"/healthz", 200}, {"/readyz", 503}} {
			mode := "normal"
			if observeOnly {
				mode = "observe"
			}
			t.Run(mode+check.path, func(t *testing.T) {
				// Do not start the node or its maintenance goroutine. Unsigned values
				// exercise the memory store only; no submission or P2P traffic occurs.
				n := loadNode(t, 2, 0, 1)
				live := types.NewTx(&types.LegacyTx{Nonce: 1})
				expired := types.NewTx(&types.LegacyTx{Nonce: 2})
				now := time.Now()
				for _, fixture := range []struct {
					tx       *types.Transaction
					inserted time.Time
				}{{live, now}, {expired, now.Add(-2 * n.cfg.CacheTTL)}} {
					if fresh, err := n.cache.add(fixture.tx, fixture.inserted); !fresh || err != nil {
						t.Fatalf("cache fixture: fresh=%v err=%v", fresh, err)
					}
				}
				// Direct inspection is deliberate: store.stats/get would themselves prune.
				assertCache := func(wantExpired bool) {
					t.Helper()
					n.cache.mu.Lock()
					defer n.cache.mu.Unlock()
					liveEntry, expiredEntry := n.cache.entries[live.Hash()], n.cache.entries[expired.Hash()]
					size, count := int(live.Size()), 1
					if wantExpired {
						size += int(expired.Size())
						count++
					}
					if liveEntry == nil || (expiredEntry != nil) != wantExpired || len(n.cache.entries) != count || len(n.cache.expiries) != count || n.cache.bytes != size {
						t.Fatalf("unexpected cache state: live=%v expired=%v count=%d heap=%d bytes=%d", liveEntry != nil, expiredEntry != nil, len(n.cache.entries), len(n.cache.expiries), n.cache.bytes)
					}
					if err := cacheInvariantLocked(n.cache); err != nil {
						t.Fatal(err)
					}
				}
				assertCache(true)
				handler := n.Handler()
				if observeOnly {
					handler = n.ObservationHandler()
				}
				server := httptest.NewServer(handler)
				defer server.Close()
				client := server.Client()
				client.Timeout = 3 * time.Second
				var before, after Stats
				readObservationCacheFixture(t, client, server.URL+"/v1/stats", http.StatusOK, &before)
				assertCache(true)
				var health Health
				readObservationCacheFixture(t, client, server.URL+check.path, check.status, &health)
				if health.CachedTransactions != 1 || health.CachedBytes != int(live.Size()) {
					t.Fatalf("health did not report only live entries: %+v", health)
				}
				assertCache(false)
				readObservationCacheFixture(t, client, server.URL+"/v1/stats", http.StatusOK, &after)
				assertCache(false)
				if before.InstanceID != after.InstanceID || before.CreatedAt != after.CreatedAt || before.Submit != (SubmitStats{}) || after.Submit != before.Submit || before.HTTP != (HTTPStats{}) || after.HTTP != before.HTTP {
					t.Fatalf("observation changed counter or instance state: before=%+v after=%+v", before, after)
				}
			})
		}
	}
}
