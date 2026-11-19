// SPDX-License-Identifier: GPL-3.0-or-later
package relay

import (
	"net/http"
	"net/http/httptest"
	"runtime"
	"testing"
	"time"
)

// Snapshot collection is the fixed-size observation core, not JSON encoding or
// a whole HTTP request. Keep its zero-allocation property under ordinary tests.
func TestStatsSnapshotAllocations(t *testing.T) {
	n := &Node{stats: runtimeStats{identity: testStatsIdentity(t)}}
	n.stats.record(submitRejectedValidation)
	var last Stats
	allocations := testing.AllocsPerRun(1000, func() { last = n.Stats() })
	if allocations != 0 {
		t.Fatalf("Stats allocated %g objects per snapshot; want zero", allocations)
	}
	if last.InstanceID != n.stats.identity.id || last.ObservedAt.IsZero() || last.Submit.RejectedValidationTotal != 1 {
		t.Fatalf("invalid allocation-test snapshot: %+v", last)
	}
	runtime.KeepAlive(last)
}

// These measurements separate snapshot work from JSON/ResponseRecorder cost.
// No sockets, peers, Submit calls, timing SLO, or remote delivery are involved.
func BenchmarkStatsObservation(b *testing.B) {
	for _, full := range []bool{false, true} {
		name := "empty_cache_20_sessions"
		if full {
			name = "full_cache_20_sessions"
		}
		b.Run(name, func(b *testing.B) {
			n := loadNode(b, 1024, 20, 1)
			if full {
				txs, _ := loadFixtures(b, 1024)
				now := time.Now()
				for _, tx := range txs {
					if fresh, err := n.cache.add(tx, now); !fresh || err != nil {
						b.Fatalf("cache fixture: %v %v", fresh, err)
					}
				}
			}
			// Synthetic counter initialization is outside timing and is not evidence of
			// a real submission. Every method below observes the same stable epoch.
			n.stats.record(submitRejectedValidation)
			b.Run("snapshot_serial", func(b *testing.B) {
				var last Stats
				b.ReportAllocs()
				b.ResetTimer()
				for i := 0; i < b.N; i++ {
					last = n.Stats()
				}
				runtime.KeepAlive(last)
			})
			b.Run("snapshot_parallel", func(b *testing.B) {
				b.ReportAllocs()
				b.ResetTimer()
				b.RunParallel(func(pb *testing.PB) {
					var last Stats
					for pb.Next() {
						last = n.Stats()
					}
					runtime.KeepAlive(last)
				})
			})
			handler := n.ObservationHandler()
			request := httptest.NewRequest(http.MethodGet, "http://localhost/v1/stats", nil)
			// The request is read-only for this GET path. Each operation allocates its
			// own recorder; those allocations deliberately remain in the measurement.
			observe := func(b *testing.B) {
				recorder := httptest.NewRecorder()
				handler.ServeHTTP(recorder, request)
				if recorder.Code != http.StatusOK || recorder.Body.Len() == 0 {
					b.Error("statistics handler did not produce a successful body")
				}
			}
			b.Run("handler_recorder_serial", func(b *testing.B) {
				b.ReportAllocs()
				b.ResetTimer()
				for i := 0; i < b.N; i++ {
					observe(b)
				}
			})
			b.Run("handler_recorder_parallel", func(b *testing.B) {
				b.ReportAllocs()
				b.ResetTimer()
				b.RunParallel(func(pb *testing.PB) {
					for pb.Next() {
						observe(b)
					}
				})
			})
		})
	}
}
