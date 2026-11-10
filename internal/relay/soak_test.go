// SPDX-License-Identifier: GPL-3.0-or-later
package relay

// This opt-in harness never starts a P2P listener or discovery. The only socket
// is a literal loopback HTTP listener. MsgPipe is synchronous in-process I/O,
// NOT TCP/RLPx, a remote txpool, inclusion, or a network delivery SLO.
import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"math/big"
	"os"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/ethereum/go-ethereum/common"
	eth "github.com/ethereum/go-ethereum/eth/protocols/eth"
	"github.com/ethereum/go-ethereum/p2p"
	"github.com/ethereum/go-ethereum/p2p/enode"
	"github.com/ethereum/go-ethereum/rlp"
)

const soakExactLimit = 65536

// Fixed-size, base-2 histograms have 256 subdivisions per power of two.
// Every observation is counted. Quantile bounds are reported after the exact
// reservoir fills; no sampling or invented exact percentiles are used.
type soakLatency struct {
	Count uint64 `json:"count"`
	MaxNS int64  `json:"max_ns"`
	bins  [64 * 256]uint64
	exact []int64
}

func (h *soakLatency) add(d time.Duration) {
	ns := int64(d)
	if ns < 0 {
		panic("negative latency")
	}
	h.Count++
	if ns > h.MaxNS {
		h.MaxNS = ns
	}
	if h.exact == nil && h.Count == 1 {
		h.exact = make([]int64, 0, soakExactLimit)
	}
	if h.Count <= soakExactLimit {
		h.exact = append(h.exact, ns)
	}
	exp := 0
	for v := uint64(ns); v > 1; v >>= 1 {
		exp++
	}
	base := uint64(1) << exp
	sub := int((uint64(ns) - min(uint64(ns), base)) * 256 / base)
	h.bins[exp*256+sub]++
}
func (h *soakLatency) report() map[string]any {
	out := map[string]any{"count": h.Count, "max_ns": h.MaxNS, "unit": "nanoseconds"}
	if h.Count == 0 {
		return out
	}
	if h.Count <= soakExactLimit {
		sorted := append([]int64(nil), h.exact...)
		sort.Slice(sorted, func(i, j int) bool { return sorted[i] < sorted[j] })
		for _, p := range []int{50, 95, 99} {
			rank := (h.Count*uint64(p) + 99) / 100
			out[fmt.Sprintf("p%d_ns", p)] = sorted[rank-1]
		}
		out["method"] = "exact nearest rank, all observations"
	} else {
		for _, p := range []int{50, 95, 99} {
			rank := (h.Count*uint64(p) + 99) / 100
			var sum uint64
			for i, n := range h.bins {
				sum += n
				if sum >= rank {
					exp, sub := i/256, i%256
					base := uint64(1) << exp
					lo := base + (base*uint64(sub)+255)/256
					hi := base + (base*uint64(sub+1)+255)/256 - 1
					if i == 0 {
						lo = 0
					}
					out[fmt.Sprintf("p%d_ns_bounds", p)] = []uint64{lo, hi}
					break
				}
			}
		}
		out["method"] = "nearest-rank containing bucket, all observations; <=0.391% relative bucket width above 256 ns"
	}
	return out
}

type soakStats struct {
	mu           sync.Mutex
	latency      map[string]*soakLatency
	counts       map[string]uint64
	httpFailures []string
}

func newSoakStats() *soakStats {
	return &soakStats{latency: make(map[string]*soakLatency), counts: make(map[string]uint64)}
}
func (s *soakStats) httpFailure(err error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.httpFailures) < 8 {
		message := err.Error()
		if len(message) > 512 {
			message = message[:512]
		}
		s.httpFailures = append(s.httpFailures, message)
	}
}
func (s *soakStats) count(k string, n uint64) { s.mu.Lock(); s.counts[k] += n; s.mu.Unlock() }
func (s *soakStats) observe(k string, d time.Duration) {
	s.mu.Lock()
	h := s.latency[k]
	if h == nil {
		h = &soakLatency{}
		s.latency[k] = h
	}
	h.add(d)
	s.mu.Unlock()
}
func (s *soakStats) report() map[string]any {
	s.mu.Lock()
	defer s.mu.Unlock()
	lat := map[string]any{}
	for k, v := range s.latency {
		lat[k] = v.report()
	}
	c := map[string]uint64{}
	for k, v := range s.counts {
		c[k] = v
	}
	return map[string]any{"counts": c, "latency": lat, "http_validation_failures": append([]string(nil), s.httpFailures...)}
}
func (s *soakStats) get(k string) uint64 { s.mu.Lock(); defer s.mu.Unlock(); return s.counts[k] }

// cacheAt is cache insertion time, NOT enqueue time. It can predate a
// duplicate retry/reconnect by minutes; cache-to-write is an upper bound on
// queue residence, never an exact queue-only latency.
type soakWrite struct {
	hash             common.Hash
	cacheAt, started time.Time
}
type soakRW struct {
	p2p.MsgReadWriter
	n       *Node
	stats   *soakStats
	kind    string
	mu      sync.Mutex
	current soakWrite
}

func (rw *soakRW) WriteMsg(msg p2p.Msg) (resultErr error) {
	if msg.Code != eth.TransactionsMsg {
		return rw.MsgReadWriter.WriteMsg(msg)
	}
	attempted := false
	defer func() {
		if resultErr != nil && !attempted {
			rw.stats.count("observer_error", 1)
		}
	}()
	// Deliberate test observer overhead: copy/decode each tiny fixture before the
	// pipe write. It is included in cache-to-write time, but not underlying I/O.
	payload, err := io.ReadAll(msg.Payload)
	if err != nil {
		return err
	}
	var packet eth.TransactionsPacket
	if err = rlp.DecodeBytes(payload, &packet); err != nil {
		return err
	}
	txs, err := packet.RawList.Items()
	if err != nil {
		return err
	}
	if len(txs) != 1 {
		return fmt.Errorf("expected one transaction, got %d", len(txs))
	}
	hash := txs[0].Hash()
	expiry := rw.n.cache.expiry(hash)
	started := time.Now()
	cacheAt := time.Time{}
	if !expiry.IsZero() {
		cacheAt = expiry.Add(-rw.n.cfg.CacheTTL)
		rw.stats.observe(rw.kind+"_cache_to_write_start", started.Sub(cacheAt))
	} else {
		rw.stats.count("missing_cache_timestamp", 1)
	}
	rw.mu.Lock()
	rw.current = soakWrite{hash, cacheAt, started}
	rw.mu.Unlock()
	attempted = true
	rw.stats.count("write_attempts", 1)
	rw.stats.count(rw.kind+"_write_attempts", 1)
	msg.Payload = bytes.NewReader(payload)
	err = rw.MsgReadWriter.WriteMsg(msg)
	completed := time.Now()
	if err == nil {
		rw.stats.count("write_completed", 1)
		rw.stats.observe(rw.kind+"_pipe_write_completed", completed.Sub(started))
		if !expiry.IsZero() && !completed.Before(expiry) {
			rw.stats.count("write_completed_after_cache_expiry", 1)
		}
	} else {
		rw.stats.count("write_failed", 1)
		rw.stats.observe(rw.kind+"_pipe_write_failed", completed.Sub(started))
	}
	return err
}
func (rw *soakRW) received(hash common.Hash, v soakWrite) error {
	now := time.Now()
	if v.hash != hash {
		return fmt.Errorf("fixture hash mismatch: %s != %s", hash, v.hash)
	}
	rw.stats.count("fixture_decoded", 1)
	rw.stats.count(rw.kind+"_fixture_decoded", 1)
	rw.stats.observe(rw.kind+"_write_start_to_fixture_decode", now.Sub(v.started))
	if !v.cacheAt.IsZero() {
		rw.stats.observe(rw.kind+"_cache_to_fixture_decode", now.Sub(v.cacheAt))
	}
	return nil
}

type soakPeer struct {
	a, b                     *p2p.MsgPipeRW
	p                        *peerSession
	official                 *eth.Peer
	writerDone, receiverDone chan struct{}
	stop                     chan struct{}
	once                     sync.Once
	stats                    *soakStats
}

func newSoakPeer(t *testing.T, n *Node, id byte, kind string, delay time.Duration, blocked <-chan struct{}, stats *soakStats) *soakPeer {
	t.Helper()
	a, b := p2p.MsgPipe()
	rw := &soakRW{MsgReadWriter: a, n: n, stats: stats, kind: kind}
	p := &peerSession{peer: p2p.NewPeer(enode.ID{id}, "soak-relay", nil), rw: rw, queue: make(chan outbound, 64), done: make(chan struct{})}
	sp := &soakPeer{a: a, b: b, p: p, writerDone: make(chan struct{}), receiverDone: make(chan struct{}), stop: make(chan struct{}), stats: stats}
	sp.official = eth.NewPeer(68, p2p.NewPeer(enode.ID{id, 1}, "official-offline-fixture", nil), b, nil, n.chain.config)
	result := make(chan error, 1)
	go func() { result <- n.handshake(p) }()
	if err := sp.official.Handshake(56, n.chain, eth.BlockRangeUpdatePacket{}, big.NewInt(1), nil); err != nil {
		a.Close()
		b.Close()
		sp.official.Close()
		t.Fatal(err)
	}
	awaitProtocol(t, result)
	n.mu.Lock()
	n.peers[p.peer.ID()] = p
	n.mu.Unlock()
	stats.count("peer_sessions", 1)
	go func() { defer close(sp.writerDone); n.writer(p) }()
	go func() {
		defer close(sp.receiverDone)
		if blocked != nil {
			select {
			case <-blocked:
			case <-sp.stop:
				return
			}
		}
		for {
			if delay > 0 {
				timer := time.NewTimer(delay)
				select {
				case <-timer.C:
				case <-sp.stop:
					timer.Stop()
					return
				}
			}
			msg, err := b.ReadMsg()
			if err != nil {
				return
			}
			if msg.Code != eth.TransactionsMsg {
				msg.Discard()
				stats.count("unexpected_fixture_code", 1)
				return
			}
			rw.mu.Lock()
			write := rw.current
			rw.mu.Unlock()
			var packet eth.TransactionsPacket
			err = msg.Decode(&packet)
			msg.Discard()
			if err != nil {
				stats.count("fixture_decode_error", 1)
				return
			}
			txs, err := packet.RawList.Items()
			if err != nil {
				stats.count("fixture_decode_error", 1)
				return
			}
			for _, tx := range txs {
				if err := rw.received(tx.Hash(), write); err != nil {
					stats.count("fixture_hash_error", 1)
					return
				}
			}
		}
	}()
	return sp
}
func (sp *soakPeer) close(t *testing.T, n *Node) {
	t.Helper()
	sp.once.Do(func() {
		n.submitMu.Lock()
		n.mu.Lock()
		if n.peers[sp.p.peer.ID()] == sp.p {
			delete(n.peers, sp.p.peer.ID())
		}
		close(sp.p.done)
		n.mu.Unlock()
		n.submitMu.Unlock()
		close(sp.stop)
		sp.a.Close()
		sp.b.Close()
		sp.official.Close()
		for _, done := range []chan struct{}{sp.writerDone, sp.receiverDone} {
			select {
			case <-done:
			case <-time.After(2 * time.Second):
				t.Error("fixture goroutine failed to stop")
			}
		}
		sp.stats.count("disconnects", 1)
		sp.stats.count("abandoned_queued_on_disconnect", uint64(len(sp.p.queue)))
		for len(sp.p.queue) > 0 {
			<-sp.p.queue
		}
	})
}

type soakSample struct {
	ElapsedMS                                                  int64  `json:"elapsed_ms"`
	HeapAlloc                                                  uint64 `json:"heap_alloc_bytes"`
	HeapInuse                                                  uint64 `json:"heap_inuse_bytes"`
	RSSBytes                                                   uint64 `json:"rss_bytes"`
	Goroutines                                                 int    `json:"goroutines"`
	CacheCount, CacheBytes, PeerSent, QueueCount, MaxPeerQueue int
}

func soakSampleNow(n *Node, start time.Time) soakSample {
	var m runtime.MemStats
	runtime.ReadMemStats(&m)
	v := soakSample{ElapsedMS: time.Since(start).Milliseconds(), HeapAlloc: m.HeapAlloc, HeapInuse: m.HeapInuse, Goroutines: runtime.NumGoroutine()}
	if data, err := os.ReadFile("/proc/self/status"); err == nil {
		for _, line := range strings.Split(string(data), "\n") {
			if strings.HasPrefix(line, "VmRSS:") {
				fields := strings.Fields(line)
				kb, _ := strconv.ParseUint(fields[1], 10, 64)
				v.RSSBytes = kb * 1024
			}
		}
	}
	n.submitMu.Lock()
	defer n.submitMu.Unlock()
	n.mu.RLock()
	defer n.mu.RUnlock()
	n.cache.mu.Lock()
	defer n.cache.mu.Unlock()
	v.CacheCount = len(n.cache.entries)
	v.CacheBytes = n.cache.bytes
	for _, p := range n.peers {
		v.PeerSent += len(p.sent)
		v.QueueCount += len(p.queue)
		v.MaxPeerQueue = max(v.MaxPeerQueue, len(p.queue))
	}
	return v
}
func soakConservation(t *testing.T, s *soakStats) {
	t.Helper()
	queued, attempted, abandoned := s.get("queue_admissions"), s.get("write_attempts"), s.get("abandoned_queued_on_disconnect")
	if attempted+abandoned > queued {
		t.Fatalf("bad accounting queued=%d attempts=%d abandoned=%d", queued, attempted, abandoned)
	}
	if s.get("observer_error") != 0 {
		t.Error("observer error invalidates expiry conservation")
	} else {
		s.count("writer_skipped_expired_by_conservation", queued-attempted-abandoned)
	}
	if s.get("write_completed")+s.get("write_failed") != attempted {
		t.Fatal("write outcomes do not sum to attempts")
	}
}

func TestSoakMsgPipe(t *testing.T) {
	if os.Getenv("RELAY_SOAK") != "1" {
		t.Skip("opt-in: RELAY_SOAK=1; see docs/SOAK.md")
	}
	duration := 5 * time.Second
	if v := os.Getenv("RELAY_SOAK_DURATION"); v != "" {
		d, err := time.ParseDuration(v)
		if err != nil || d < 3*time.Second || d > time.Hour {
			t.Fatal("RELAY_SOAK_DURATION must be 3s..1h per occupancy scenario")
		}
		duration = d
	}
	seed := uint64(20261110)
	if v := os.Getenv("RELAY_SOAK_SEED"); v != "" {
		n, err := strconv.ParseUint(v, 10, 64)
		if err != nil {
			t.Fatal(err)
		}
		seed = n
	}
	report := map[string]any{"transport": "in-process synchronous BSC MsgPipe (not TCP/RLPx)", "http": "127.0.0.1 TCP only", "go": runtime.Version(), "gomaxprocs": runtime.GOMAXPROCS(0), "seed": seed, "duration_per_occupancy_s": duration.Seconds(), "workers": 16, "offered_requests_per_second": 800, "cache_limit": 1024, "peer_queue_limit": 64, "synthetic_only": true, "harness_revision": "http-fail-closed-v2"}
	// Write even after a failure. CI output and the requested report retain failed
	// outcomes; no retry can silently overwrite a prior run unless the caller
	// deliberately supplies the same filename.
	t.Cleanup(func() {
		data, err := json.MarshalIndent(report, "", "  ")
		if err != nil {
			t.Error(err)
			return
		}
		if path := os.Getenv("RELAY_SOAK_REPORT"); path != "" {
			f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
			if err != nil {
				t.Error(err)
				return
			}
			_, err = f.Write(append(data, '\n'))
			closeErr := f.Close()
			if err != nil || closeErr != nil {
				t.Errorf("report write: %v %v", err, closeErr)
			}
		}
		t.Logf("SOAK_REPORT %s", data)
	})
	for _, occupancy := range []int{900, 1024} {
		t.Run(fmt.Sprintf("occupancy_%d", occupancy), func(t *testing.T) {
			n := protocolNode(t)
			stats := newSoakStats()
			t.Cleanup(func() {
				key := fmt.Sprintf("occupancy_%d", occupancy)
				if report[key] == nil {
					result := stats.report()
					result["passed"] = false
					result["partial_due_to_early_failure"] = true
					report[key] = result
				}
			})
			api := newSoakAPI(t, n, stats)
			bodies := soakBodies(t, 1025)
			fast := newSoakPeer(t, n, 1, "fast", 0, nil, stats)
			t.Cleanup(func() { fast.close(t, n) })
			// Real API admission populates the cache; no direct insertion or validation
			// bypass. Wait for each write to avoid an unmeasured warmup queue overflow.
			for i := 0; i < occupancy; i++ {
				status, _, err := api.submit(context.Background(), bodies[i], soakHTTPPrefill)
				if err != nil || status != 202 {
					t.Fatalf("prefill %d status %d: %v", i, status, err)
				}
				deadline := time.Now().Add(time.Second)
				for stats.get("fixture_decoded") < uint64(i+1) {
					if time.Now().After(deadline) {
						t.Fatal("prefill did not decode")
					}
					time.Sleep(time.Millisecond)
				}
			}
			if h := n.Health(); h.CachedTransactions != occupancy {
				t.Fatalf("actual occupancy %d != %d", h.CachedTransactions, occupancy)
			}
			// Warmup counters stay in conservation; latency series are reset only after
			// the sole warmup writer has completed, so measurement never drops an event.
			warmupDeadline := time.Now().Add(time.Second)
			for stats.get("write_completed") < uint64(occupancy) {
				if time.Now().After(warmupDeadline) {
					t.Fatal("prefill writer completion timed out")
				}
				time.Sleep(time.Millisecond)
			}
			fast.close(t, n)
			fast = newSoakPeer(t, n, 1, "fast", 0, nil, stats)
			stats.mu.Lock()
			stats.latency = make(map[string]*soakLatency)
			stats.mu.Unlock()
			slow := newSoakPeer(t, n, 2, "slow", 75*time.Millisecond, nil, stats)
			t.Cleanup(func() { slow.close(t, n) })
			blocked := make(chan struct{})
			churn := newSoakPeer(t, n, 3, "churn", 0, blocked, stats)
			t.Cleanup(func() { churn.close(t, n) })
			start := time.Now()
			runtime.GC()
			samples := []soakSample{soakSampleNow(n, start)}
			baseG := runtime.NumGoroutine()
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			var wg sync.WaitGroup
			// Scheduling is concurrent, so seed fixes index permutations, not ordering.
			for worker := 0; worker < 16; worker++ {
				wg.Add(1)
				go func(w int) {
					defer wg.Done()
					ticker := time.NewTicker(20 * time.Millisecond)
					defer ticker.Stop()
					i := uint64(w) + seed%uint64(occupancy)
					for {
						select {
						case <-ctx.Done():
							return
						case <-ticker.C:
							idx := int(i % uint64(occupancy))
							i += 16

							api.submit(context.Background(), bodies[idx], soakHTTPActive)
						}
					}
				}(worker)
			}
			ticker := time.NewTicker(time.Second)
			defer ticker.Stop()
			end := time.NewTimer(duration)
			defer end.Stop()
			nextChurn := 2 * time.Second
		loop:
			for {
				select {
				case <-end.C:
					break loop
				case <-ticker.C:
					n.prune(time.Now())
					samples = append(samples, soakSampleNow(n, start))
					assertLoadBounds(t, n)
					if time.Since(start) >= nextChurn {
						fast.close(t, n)
						fast = newSoakPeer(t, n, 1, "fast", 0, nil, stats)
						churn.close(t, n)
						churn = newSoakPeer(t, n, 3, "churn", 0, blocked, stats)
						stats.count("reconnects", 1)
						nextChurn += 2 * time.Second
					}
				}
			}
			cancel()
			wg.Wait()
			measured := time.Since(start)
			activeRequests := stats.get("http_requests")
			if occupancy == 1024 && n.Health().CachedTransactions != 1024 {
				stats.count("full_cache_probe_skipped_not_full", 1)
			}
			if occupancy == 1024 && n.Health().CachedTransactions == 1024 {
				for i := 0; i < 16; i++ {
					status, _, err := api.submit(context.Background(), bodies[1024], soakHTTPCacheFull)
					if err != nil || status != 429 {
						t.Errorf("full cache overflow status %d: %v", status, err)
					}
				}
			}
			samples = append(samples, soakSampleNow(n, start))
			churn.close(t, n)
			slow.close(t, n)
			fast.close(t, n)
			status, _, err := api.submit(context.Background(), bodies[0], soakHTTPDisconnected)
			if err != nil || status != 503 {
				t.Errorf("disconnected status %d: %v", status, err)
			}
			api.client.CloseIdleConnections()
			runtime.GC()
			samples = append(samples, soakSampleNow(n, start))
			assertLoadBounds(t, n)
			if err := soakValidateScenario(stats); err != nil {
				t.Error(err)
			}
			soakConservation(t, stats)
			if stats.get("reconnects") == 0 || stats.get("write_failed") == 0 {
				t.Error("did not exercise disconnect/reconnect with blocked write")
			}
			if runtime.NumGoroutine() > baseG+8 {
				t.Errorf("goroutines did not return near baseline: initial=%d current=%d", baseG, runtime.NumGoroutine())
			}
			result := stats.report()
			result["samples"] = samples
			result["actual_prefill"] = occupancy
			result["active_load_seconds"] = measured.Seconds()
			result["active_http_requests"] = activeRequests
			result["active_http_requests_per_second"] = float64(activeRequests) / measured.Seconds()
			result["warmup_queue_admissions"] = occupancy
			result["passed"] = !t.Failed()
			report[fmt.Sprintf("occupancy_%d", occupancy)] = result
		})
	}
	t.Run("expiry_and_queue_full", func(t *testing.T) {
		n := protocolNode(t)
		n.cfg.CacheTTL = 250 * time.Millisecond
		n.cache.ttl = n.cfg.CacheTTL
		stats := newSoakStats()
		t.Cleanup(func() {
			if report["expiry_and_queue_full"] == nil {
				result := stats.report()
				result["passed"] = false
				result["partial_due_to_early_failure"] = true
				report["expiry_and_queue_full"] = result
			}
		})
		api := newSoakAPI(t, n, stats)
		bodies := soakBodies(t, 80)
		gate := make(chan struct{})
		p := newSoakPeer(t, n, 4, "expiry", 0, gate, stats)
		t.Cleanup(func() { p.close(t, n) })
		status, _, err := api.submit(context.Background(), bodies[0], soakHTTPAdmission)
		if err != nil || status != 202 {
			t.Fatalf("first status %d: %v", status, err)
		}
		deadline := time.Now().Add(time.Second)
		for stats.get("write_attempts") == 0 {
			if time.Now().After(deadline) {
				t.Fatal("no blocked write")
			}
			time.Sleep(time.Millisecond)
		}
		for i := 1; i <= 64; i++ {
			status, _, err = api.submit(context.Background(), bodies[i], soakHTTPAdmission)
			if err != nil || status != 202 {
				t.Fatalf("queue fill %d status %d: %v", i, status, err)
			}
		}
		status, _, err = api.submit(context.Background(), bodies[65], soakHTTPQueueFull)
		if err != nil || status != 429 {
			t.Fatalf("full queues status %d: %v", status, err)
		}
		time.Sleep(300 * time.Millisecond)
		close(gate)
		deadline = time.Now().Add(time.Second)
		for stats.get("write_completed") < 1 || len(p.p.queue) > 0 {
			if time.Now().After(deadline) {
				t.Fatal("expired queue failed to drain")
			}
			time.Sleep(time.Millisecond)
		}
		p.close(t, n)
		if err := soakValidateScenario(stats); err != nil {
			t.Error(err)
		}
		if stats.get("fixture_decoded") != 1 {
			t.Errorf("expiry fixture decoded %d, want 1", stats.get("fixture_decoded"))
		}
		soakConservation(t, stats)
		if stats.get("writer_skipped_expired_by_conservation") != 64 || stats.get("write_completed_after_cache_expiry") != 1 {
			t.Errorf("expiry accounting skipped=%d late_completed=%d", stats.get("writer_skipped_expired_by_conservation"), stats.get("write_completed_after_cache_expiry"))
		}
		result := stats.report()
		result["ttl_ms"] = 250
		result["passed"] = !t.Failed()
		report["expiry_and_queue_full"] = result
	})
	report["passed"] = !t.Failed()
}

// The reporting math itself is ordinary CI coverage; the timed harness is not.
func TestSoakQuantiles(t *testing.T) {
	var h soakLatency
	for i := 1; i <= 100; i++ {
		h.add(time.Duration(i))
	}
	r := h.report()
	for k, want := range map[string]int64{"p50_ns": 50, "p95_ns": 95, "p99_ns": 99} {
		if r[k] != want {
			t.Fatalf("%s=%v", k, r[k])
		}
	}
	var many soakLatency
	for i := 0; i < soakExactLimit+1; i++ {
		many.add(123456 * time.Nanosecond)
	}
	r = many.report()
	for _, p := range []int{50, 95, 99} {
		b := r[fmt.Sprintf("p%d_ns_bounds", p)].([]uint64)
		if b[0] > 123456 || b[1] < 123456 || math.Abs(float64(b[1]-b[0])) > 1024 {
			t.Fatal("invalid histogram interval", b)
		}
	}
}
