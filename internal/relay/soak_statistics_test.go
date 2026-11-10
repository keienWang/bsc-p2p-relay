// SPDX-License-Identifier: GPL-3.0-or-later
package relay

import (
	"fmt"
	"math/rand"
	"sort"
	"testing"
	"time"
)

func TestSoakMixedHistogramQuantiles(t *testing.T) {
	for _, count := range []int{1, 100, soakExactLimit, soakExactLimit + 1, 100000} {
		t.Run(fmt.Sprint(count), func(t *testing.T) {
			var h soakLatency
			rng := rand.New(rand.NewSource(21))
			values := make([]int64, count)
			for i := range values {
				v := rng.Int63n(int64(time.Hour))
				if i%3 == 0 {
					v = rng.Int63n(1024)
				}
				if i%5 == 0 {
					v = int64(1) << uint(i%42)
				}
				values[i] = v
				h.add(time.Duration(v))
			}
			sort.Slice(values, func(i, j int) bool { return values[i] < values[j] })
			r := h.report()
			if h.Count != uint64(count) || len(h.exact) > soakExactLimit {
				t.Fatal("count/storage bound mismatch")
			}
			for _, p := range []int{50, 95, 99} {
				want := values[(count*p+99)/100-1]
				if count <= soakExactLimit {
					if got := r[fmt.Sprintf("p%d_ns", p)]; got != want {
						t.Fatalf("p%d got %v want %v", p, got, want)
					}
				} else {
					bounds := r[fmt.Sprintf("p%d_ns_bounds", p)].([]uint64)
					if bounds[0] > uint64(want) || bounds[1] < uint64(want) {
						t.Fatalf("p%d bounds %v exclude %v", p, bounds, want)
					}
				}
			}
		})
	}
}
