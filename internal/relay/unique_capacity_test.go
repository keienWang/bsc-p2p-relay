// SPDX-License-Identifier: GPL-3.0-or-later
package relay

// These fixtures offer new signed hashes, not repeated requests to a prefill.
// No P2P server, discovery, RPC, wallet, funded account or public network is used.
import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/big"
	"net/http"
	"os"
	"runtime"
	"sort"
	"strconv"
	"sync"
	"testing"
	"time"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/crypto"
)

type uniqueRequest struct {
	soakRequest
	nonce uint64
	size  int
}

type uniqueGenerator struct {
	mu     sync.Mutex
	key    *ecdsa.PrivateKey
	sender common.Address
	nonce  uint64
}

func newUniqueGenerator(t *testing.T) *uniqueGenerator {
	t.Helper()
	// Public, predictable fixture key: 0x02 repeated 32 times. Never fund it.
	key, err := crypto.ToECDSA(bytes.Repeat([]byte{2}, 32))
	if err != nil {
		t.Fatal(err)
	}
	return &uniqueGenerator{key: key, sender: crypto.PubkeyToAddress(key.PublicKey)}
}

func (g *uniqueGenerator) next(t *testing.T, dataBytes int) uniqueRequest {
	t.Helper()
	g.mu.Lock()
	nonce := g.nonce
	g.nonce++
	g.mu.Unlock()
	// Also give large-payload fixtures enough intrinsic gas; stateful acceptance
	// remains unproved and is not inferred from this relay's stateless validator.
	tx := types.NewTransaction(nonce, common.Address{19: 1}, big.NewInt(0), uint64(21000+16*dataBytes), big.NewInt(1), make([]byte, dataBytes))
	tx, err := types.SignTx(tx, types.LatestSignerForChainID(big.NewInt(56)), g.key)
	if err != nil {
		t.Fatal(err)
	}
	raw, err := tx.MarshalBinary()
	if err != nil {
		t.Fatal(err)
	}
	return uniqueRequest{soakRequest{[]byte(`{"raw_tx":"0x` + hex.EncodeToString(raw) + `"}`), tx.Hash(), g.sender}, nonce, int(tx.Size())}
}

// Every HTTP call declares exactly which outcomes are allowed. A new hash must
// never return duplicate=true, and retries are counted separately even after TTL
// makes the cache call them fresh. Error responses expose no hash/duplicate flag.
type uniqueContract struct {
	accept               bool
	duplicate            bool
	minQueued, maxQueued int
	rejections           map[int]string
}

func uniqueAccept(duplicate bool, minQueued, maxQueued int) uniqueContract {
	return uniqueContract{accept: true, duplicate: duplicate, minQueued: minQueued, maxQueued: maxQueued}
}

func uniqueReject(status int, err error) uniqueContract {
	return uniqueContract{rejections: map[int]string{status: err.Error()}}
}

func uniqueValidateHTTP(status int, contentType string, payload []byte, fixture soakRequest, contract uniqueContract) (Submission, string, error) {
	if status == 202 {
		if !contract.accept {
			return Submission{}, "", errors.New("202 forbidden in this phase")
		}
		sub, _, reason, err := soakValidateHTTP(status, contentType, payload, fixture, soakHTTPActive)
		if err != nil {
			return sub, "", fmt.Errorf("%s: %w", reason, err)
		}
		if sub.Duplicate != contract.duplicate || sub.QueuedPeers < contract.minQueued || sub.QueuedPeers > contract.maxQueued {
			return sub, "", fmt.Errorf("unexpected admission: %+v", sub)
		}
		return sub, "", nil
	}
	expected, allowed := contract.rejections[status]
	if !allowed {
		return Submission{}, "", fmt.Errorf("unexpected HTTP status %d", status)
	}
	var phase soakHTTPPhase
	switch expected {
	case ErrCacheFull.Error():
		phase = soakHTTPCacheFull
	case ErrQueuesFull.Error():
		phase = soakHTTPQueueFull
	case ErrNoPeers.Error():
		phase = soakHTTPDisconnected
	case ErrAdmissionExpired.Error(), "too many concurrent requests":
		phase = soakHTTPActive
	default:
		return Submission{}, "", errors.New("unknown rejection contract")
	}
	_, rejection, reason, err := soakValidateHTTP(status, contentType, payload, fixture, phase)
	if err != nil {
		return Submission{}, "", fmt.Errorf("%s: %w", reason, err)
	}
	if rejection != expected {
		return Submission{}, "", fmt.Errorf("rejection %q != expected %q", rejection, expected)
	}
	return Submission{}, rejection, nil
}

type uniqueCounts struct {
	Attempts      uint64            `json:"attempts"`
	OfferedUnique uint64            `json:"offered_unique_hashes"`
	Retries       uint64            `json:"old_hash_retry_requests"`
	Responses     uint64            `json:"responses"`
	ClientErrors  uint64            `json:"client_errors"`
	Invalid       uint64            `json:"invalid_responses"`
	Accepted      uint64            `json:"accepted_202"`
	Duplicate     uint64            `json:"accepted_duplicate_true"`
	QueuedPeers   uint64            `json:"queue_admissions"`
	Rejected429   uint64            `json:"rejected_429"`
	Rejected503   uint64            `json:"rejected_503"`
	Statuses      map[string]uint64 `json:"response_statuses"`
	RejectReasons map[string]uint64 `json:"rejection_reasons"`
}

func newUniqueCounts() *uniqueCounts {
	return &uniqueCounts{Statuses: make(map[string]uint64), RejectReasons: make(map[string]uint64)}
}

type uniqueHarness struct {
	n        *Node
	api      *soakAPI
	stats    *soakStats
	gen      *uniqueGenerator
	start    time.Time
	mu       sync.Mutex
	ledger   map[common.Hash]*uniqueHashLedger
	counts   *uniqueCounts
	phases   map[string]*uniqueCounts
	failures []string
	samples  []soakSample
	peers    []*soakPeer
	stream   map[string]any
}

// AcceptedEver and RejectedEver overlap when a rejected hash is later retried.
// They must never be added to calculate the offered-unique denominator.
type uniqueHashLedger struct {
	Nonce             uint64   `json:"nonce"`
	FirstPhase        string   `json:"first_offer_phase"`
	FirstStatus       int      `json:"first_offer_status"`
	FirstValid        bool     `json:"first_offer_valid_response"`
	FirstOfferNS      int64    `json:"first_offer_ns_since_scenario_start"`
	FirstResponseNS   int64    `json:"first_response_ns_since_scenario_start"`
	Attempts          uint64   `json:"attempts"`
	RetryAttempts     uint64   `json:"retry_attempts"`
	LastStatus        int      `json:"last_attempt_status"`
	LastValid         bool     `json:"last_attempt_valid_response"`
	AcceptedEver      bool     `json:"ever_accepted_202"`
	RejectedEver      bool     `json:"ever_rejected_429_or_503"`
	QueueAdmissions   uint64   `json:"queue_admissions"`
	FirstCacheExpiry  string   `json:"first_observed_cache_deadline,omitempty"`
	FinalCacheExpiry  string   `json:"final_observed_cache_deadline,omitempty"`
	FinalState        string   `json:"final_local_admission_state"`
	FinalCoveredPeers []string `json:"final_unexpired_queued_peer_ids,omitempty"`
}

func newUniqueHarness(t *testing.T, count, cacheBytes int, ttl time.Duration, report map[string]any, name string) *uniqueHarness {
	t.Helper()
	cfg := DefaultConfig()
	cfg.NoDiscovery, cfg.ListenAddr = true, "127.0.0.1:0"
	cfg.CacheCount, cfg.CacheBytes, cfg.CacheTTL = count, cacheBytes, ttl
	n, err := New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	h := &uniqueHarness{n: n, stats: newSoakStats(), gen: newUniqueGenerator(t), start: time.Now(), ledger: make(map[common.Hash]*uniqueHashLedger), counts: newUniqueCounts(), phases: make(map[string]*uniqueCounts)}
	h.api = newSoakAPI(t, n, h.stats)
	// Runs on success and early Fatal alike. Teardown completes all writes before
	// conservation; partial failed scenarios remain in the report, not discarded.
	t.Cleanup(func() {
		h.sample(t)
		h.finalizeLedger()
		for _, p := range h.peers {
			p.close(t, n)
		}
		h.audit(t)
		if err := uniqueTransportConservation(h.stats); err != nil {
			t.Error(err)
		}
		if h.stats.get("write_completed") != n.writes.Load() || h.stats.get("write_failed") != n.writeFailures.Load() {
			t.Error("transport observer and production writer counters differ")
		}
		result := map[string]any{
			"passed": !t.Failed(), "cache_count_limit": cfg.CacheCount, "cache_serialized_bytes_limit": cfg.CacheBytes,
			"cache_ttl_ms": cfg.CacheTTL.Milliseconds(), "scenario_elapsed_seconds_including_setup_cleanup": time.Since(h.start).Seconds(),
			"http": h.counts, "phases": h.phases, "http_validation_failures": h.failures,
			"transport": h.stats.report(), "samples": h.samples, "per_hash_ledger": h.ledger, "ledger_summary": h.ledgerSummary(), "stream_measurement": h.stream,
		}
		report[name] = result
	})
	return h
}

func (h *uniqueHarness) peer(t *testing.T, id byte, kind string, blocked <-chan struct{}) *soakPeer {
	t.Helper()
	p := newSoakPeer(t, h.n, id, kind, 0, blocked, h.stats)
	h.peers = append(h.peers, p)
	return p
}

func (h *uniqueHarness) sample(t *testing.T) {
	t.Helper()
	if err := uniqueBounds(h.n); err != nil {
		t.Error(err)
	}
	v := soakSampleNow(h.n, h.start)
	h.mu.Lock()
	h.samples = append(h.samples, v)
	h.mu.Unlock()
}

func (h *uniqueHarness) recordFailure(err error) {
	if len(h.failures) < 8 {
		message := err.Error()
		if len(message) > 512 {
			message = message[:512]
		}
		h.failures = append(h.failures, message)
	}
}

func (h *uniqueHarness) submit(f uniqueRequest, label string, retry bool, contract uniqueContract) (int, Submission, error) {
	h.mu.Lock()
	phase := h.phases[label]
	if phase == nil {
		phase = newUniqueCounts()
		h.phases[label] = phase
	}
	l, exists := h.ledger[f.hash]
	if exists != retry {
		h.mu.Unlock()
		return 0, Submission{}, fmt.Errorf("hash %s mislabeled retry=%v", f.hash, retry)
	}
	if !exists {
		l = &uniqueHashLedger{Nonce: f.nonce, FirstPhase: label, FirstOfferNS: time.Since(h.start).Nanoseconds()}
		h.ledger[f.hash] = l
	}
	l.Attempts++
	if retry {
		l.RetryAttempts++
	}
	for _, c := range []*uniqueCounts{h.counts, phase} {
		c.Attempts++
		if retry {
			c.Retries++
		} else {
			c.OfferedUnique++
		}
	}
	h.mu.Unlock()
	req, err := http.NewRequestWithContext(context.Background(), http.MethodPost, h.api.server.URL+"/v1/transactions", bytes.NewReader(f.body))
	if err == nil {
		req.Header.Set("Content-Type", "application/json")
	}
	var resp *http.Response
	start := time.Now()
	if err == nil {
		resp, err = h.api.client.Do(req)
	}
	if err != nil {
		h.mu.Lock()
		h.counts.ClientErrors++
		phase.ClientErrors++
		l.LastStatus, l.LastValid = 0, false
		h.recordFailure(err)
		h.mu.Unlock()
		return 0, Submission{}, err
	}
	payload, readErr := io.ReadAll(io.LimitReader(resp.Body, 8193))
	closeErr := resp.Body.Close()
	h.stats.observe("unique_http_roundtrip_"+strconv.Itoa(resp.StatusCode), time.Since(start))
	var sub Submission
	var rejection string
	if readErr != nil || closeErr != nil {
		err = errors.Join(readErr, closeErr)
	} else if len(payload) > 8192 {
		err = errors.New("response exceeds 8192 bytes")
	} else {
		sub, rejection, err = uniqueValidateHTTP(resp.StatusCode, resp.Header.Get("Content-Type"), payload, f.soakRequest, contract)
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	l.LastStatus, l.LastValid = resp.StatusCode, err == nil
	if !retry {
		l.FirstStatus, l.FirstValid = resp.StatusCode, err == nil
		l.FirstResponseNS = time.Since(h.start).Nanoseconds()
	}
	if err == nil {
		if resp.StatusCode == 202 {
			l.AcceptedEver = true
			l.QueueAdmissions += uint64(sub.QueuedPeers)
			if expiry := h.n.cache.expiry(f.hash); l.FirstCacheExpiry == "" && !expiry.IsZero() {
				l.FirstCacheExpiry = expiry.UTC().Format(time.RFC3339Nano)
			}
		} else {
			l.RejectedEver = true
		}
	}
	for _, c := range []*uniqueCounts{h.counts, phase} {
		c.Responses++
		c.Statuses[strconv.Itoa(resp.StatusCode)]++
		if err != nil {
			c.Invalid++
			continue
		}
		switch resp.StatusCode {
		case 202:
			c.Accepted++
			c.QueuedPeers += uint64(sub.QueuedPeers)
			if sub.Duplicate {
				c.Duplicate++
			}
		case 429:
			c.Rejected429++
			c.RejectReasons[rejection]++
		case 503:
			c.Rejected503++
			c.RejectReasons[rejection]++
		}
	}
	if err != nil {
		err = fmt.Errorf("%s nonce=%d HTTP %d: %w", label, f.nonce, resp.StatusCode, err)
		h.recordFailure(err)
	} else if resp.StatusCode == 202 {
		h.stats.count("queue_admissions", uint64(sub.QueuedPeers))
	}
	return resp.StatusCode, sub, err
}

func (h *uniqueHarness) mustSubmit(t *testing.T, f uniqueRequest, label string, retry bool, contract uniqueContract) (int, Submission) {
	t.Helper()
	status, sub, err := h.submit(f, label, retry, contract)
	if err != nil {
		t.Fatal(err)
	}
	return status, sub
}

func uniqueAuditCounts(c *uniqueCounts) error {
	var statuses, rejects uint64
	for _, n := range c.Statuses {
		statuses += n
	}
	for _, n := range c.RejectReasons {
		rejects += n
	}
	if c.Attempts != c.OfferedUnique+c.Retries || c.Attempts != c.Responses+c.ClientErrors || c.Responses != statuses || c.Responses != c.Accepted+c.Rejected429+c.Rejected503+c.Invalid || rejects != c.Rejected429+c.Rejected503 || c.Duplicate > c.Accepted {
		return fmt.Errorf("HTTP conservation failure: %+v", c)
	}
	if c.ClientErrors != 0 || c.Invalid != 0 {
		return errors.New("client errors or invalid HTTP responses fail closed")
	}
	return nil
}

func (h *uniqueHarness) audit(t *testing.T) {
	t.Helper()
	if err := uniqueAuditCounts(h.counts); err != nil {
		t.Error(err)
	}
	// Check every field, not just request totals, partitions into named phases.
	sum := newUniqueCounts()
	for label, c := range h.phases {
		if err := uniqueAuditCounts(c); err != nil {
			t.Errorf("%s: %v", label, err)
		}
		sum.Attempts += c.Attempts
		sum.OfferedUnique += c.OfferedUnique
		sum.Retries += c.Retries
		sum.Responses += c.Responses
		sum.ClientErrors += c.ClientErrors
		sum.Invalid += c.Invalid
		sum.Accepted += c.Accepted
		sum.Duplicate += c.Duplicate
		sum.QueuedPeers += c.QueuedPeers
		sum.Rejected429 += c.Rejected429
		sum.Rejected503 += c.Rejected503
		for k, n := range c.Statuses {
			sum.Statuses[k] += n
		}
		for k, n := range c.RejectReasons {
			sum.RejectReasons[k] += n
		}
	}
	a, _ := json.Marshal(sum)
	b, _ := json.Marshal(h.counts)
	if !bytes.Equal(a, b) {
		t.Error("phase totals do not equal all HTTP totals")
	}
	if h.counts.OfferedUnique != uint64(len(h.ledger)) || h.counts.QueuedPeers != h.stats.get("queue_admissions") {
		t.Error("unique hash or queue denominator mismatch")
	}
	var attempts, retries, queued, firstAccepted, firstRejected, firstInvalid uint64
	for _, l := range h.ledger {
		attempts += l.Attempts
		retries += l.RetryAttempts
		queued += l.QueueAdmissions
		if l.Attempts != 1+l.RetryAttempts {
			t.Error("per-hash first/retry denominator mismatch")
		}
		switch {
		case !l.FirstValid:
			firstInvalid++
		case l.FirstStatus == 202:
			firstAccepted++
		case l.FirstStatus == 429 || l.FirstStatus == 503:
			firstRejected++
		default:
			t.Error("unknown first-offer outcome")
		}
	}
	if attempts != h.counts.Attempts || retries != h.counts.Retries || queued != h.counts.QueuedPeers || firstAccepted+firstRejected+firstInvalid != h.counts.OfferedUnique {
		t.Error("per-hash ledger conservation mismatch")
	}
	for _, key := range []string{"fixture_decode_error", "fixture_hash_error", "unexpected_fixture_code", "observer_error"} {
		if h.stats.get(key) != 0 {
			t.Errorf("%s=%d", key, h.stats.get(key))
		}
	}
}

func (h *uniqueHarness) finalizeLedger() {
	now := time.Now()
	h.n.submitMu.Lock()
	defer h.n.submitMu.Unlock()
	h.n.mu.RLock()
	defer h.n.mu.RUnlock()
	for hash, l := range h.ledger {
		expiry := h.n.cache.expiry(hash)
		if !expiry.IsZero() {
			l.FinalCacheExpiry = expiry.UTC().Format(time.RFC3339Nano)
		}
		switch {
		case !l.LastValid:
			l.FinalState = "incomplete_or_invalid_last_response"
		case !l.AcceptedEver:
			l.FinalState = "never_accepted"
		case now.Before(expiry):
			l.FinalState = "accepted_and_unexpired_in_local_cache"
		default:
			l.FinalState = "accepted_previously_no_unexpired_cache_entry"
		}
		for id, p := range h.n.peers {
			if deadline, ok := p.sent[hash]; ok && now.Before(deadline) {
				l.FinalCoveredPeers = append(l.FinalCoveredPeers, id.String())
			}
		}
		sort.Strings(l.FinalCoveredPeers)
	}
}

func (h *uniqueHarness) ledgerSummary() map[string]any {
	first := make(map[string]uint64)
	final := make(map[string]uint64)
	var accepted, rejected, both uint64
	for _, l := range h.ledger {
		if l.FirstValid {
			first[strconv.Itoa(l.FirstStatus)]++
		} else {
			first["invalid_or_client_error"]++
		}
		final[l.FinalState]++
		if l.AcceptedEver {
			accepted++
		}
		if l.RejectedEver {
			rejected++
		}
		if l.AcceptedEver && l.RejectedEver {
			both++
		}
	}
	return map[string]any{"distinct_hashes": len(h.ledger), "first_offer_outcomes": first, "final_local_states": final, "ever_accepted_hashes": accepted, "ever_rejected_hashes": rejected, "both_accepted_and_rejected_hashes": both, "warning": "ever-accepted and ever-rejected sets overlap; only first-offer outcomes partition distinct hashes"}
}

// Error returns, rather than Fatal, ensure teardown records invalid scenarios.
func uniqueBounds(n *Node) error {
	n.submitMu.Lock()
	defer n.submitMu.Unlock()
	n.mu.RLock()
	defer n.mu.RUnlock()
	n.cache.mu.Lock()
	defer n.cache.mu.Unlock()
	var total int
	for _, e := range n.cache.entries {
		total += e.size
	}
	if len(n.cache.entries) > n.cfg.CacheCount || total != n.cache.bytes || total > n.cfg.CacheBytes {
		return fmt.Errorf("cache bound/accounting: count=%d bytes=%d actual=%d", len(n.cache.entries), n.cache.bytes, total)
	}
	for _, p := range n.peers {
		if len(p.sent) > n.cfg.CacheCount || cap(p.queue) != 64 || len(p.queue) > 64 {
			return errors.New("per-peer count/queue bound violated")
		}
	}
	return nil
}

func uniqueTransportConservation(s *soakStats) error {
	queued, attempted, abandoned := s.get("queue_admissions"), s.get("write_attempts"), s.get("abandoned_queued_on_disconnect")
	if s.get("observer_error") != 0 || attempted+abandoned > queued {
		return fmt.Errorf("cannot infer expiry skips: queued=%d attempted=%d abandoned=%d observer_errors=%d", queued, attempted, abandoned, s.get("observer_error"))
	}
	if s.get("write_completed")+s.get("write_failed") != attempted {
		return errors.New("write completions + failures != attempts")
	}
	s.count("writer_skipped_expired_by_conservation", queued-attempted-abandoned)
	return nil
}

func uniqueWait(t *testing.T, description string, ready func() bool) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for !ready() {
		if time.Now().After(deadline) {
			t.Fatalf("timed out: %s", description)
		}
		time.Sleep(time.Millisecond)
	}
}

func (h *uniqueHarness) awaitWrites(t *testing.T, n uint64) {
	t.Helper()
	uniqueWait(t, "writer completions and fixture decodes", func() bool {
		return h.stats.get("write_completed") == n && h.stats.get("fixture_decoded") == n && h.n.writes.Load() == n
	})
}

func uniqueReport(t *testing.T, mode string) map[string]any {
	t.Helper()
	report := map[string]any{"harness_revision": "unique-capacity-v1", "mode": mode, "go": runtime.Version(), "gomaxprocs": runtime.GOMAXPROCS(0), "synthetic_only": true, "transport": "loopback HTTP and in-process official BSC handshake/MsgPipe packet decoder, not remote txpool acceptance", "peer_queue_limit": 64}
	t.Cleanup(func() {
		report["passed"] = !t.Failed()
		data, err := json.MarshalIndent(report, "", "  ")
		if err != nil {
			t.Error(err)
			return
		}
		if path := os.Getenv("RELAY_UNIQUE_REPORT"); path != "" {
			f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
			if err != nil {
				t.Error(err)
				return
			}
			_, writeErr := f.Write(append(data, '\n'))
			if err := errors.Join(writeErr, f.Close()); err != nil {
				t.Error(err)
			}
		}
		t.Logf("UNIQUE_CAPACITY_REPORT %s", data)
	})
	return report
}

func TestUniqueCapacity(t *testing.T) {
	report := uniqueReport(t, "accelerated logic proof; short TTL is not a default-120s measurement")
	defaults := DefaultConfig()
	t.Run("no_peers", func(t *testing.T) {
		h := newUniqueHarness(t, 4, defaults.CacheBytes, defaults.CacheTTL, report, t.Name())
		h.mustSubmit(t, h.gen.next(t, 0), "new_no_peers", false, uniqueReject(503, ErrNoPeers))
		if n, b := h.n.cache.stats(); n != 0 || b != 0 {
			t.Fatal("503 populated cache")
		}
	})
	t.Run("count_limit", func(t *testing.T) {
		h := newUniqueHarness(t, 4, defaults.CacheBytes, defaults.CacheTTL, report, t.Name())
		h.peer(t, 1, "fast", nil)
		var first uniqueRequest
		bytesExpected := 0
		for i := 0; i < 8; i++ {
			f := h.gen.next(t, 0)
			if i == 0 {
				first = f
			}
			if i < 4 {
				h.mustSubmit(t, f, "new_count_admitted", false, uniqueAccept(false, 1, 1))
				bytesExpected += f.size
			} else {
				h.mustSubmit(t, f, "new_count_rejected", false, uniqueReject(429, ErrCacheFull))
			}
			h.sample(t)
		}
		h.awaitWrites(t, 4)
		expiry := h.n.cache.expiry(first.hash)
		h.mustSubmit(t, first, "old_hash_retry_at_full_count", true, uniqueAccept(true, 0, 0))
		if n, b := h.n.cache.stats(); n != 4 || b != bytesExpected {
			t.Fatalf("after writes count=%d bytes=%d want 4/%d", n, b, bytesExpected)
		}
		if h.n.cache.expiry(first.hash) != expiry {
			t.Fatal("retry extended retention")
		}
	})
	t.Run("bytes_before_count", func(t *testing.T) {
		h := newUniqueHarness(t, 16, MaxRawTransaction, defaults.CacheTTL, report, t.Name())
		h.peer(t, 1, "fast", nil)
		first := h.gen.next(t, 70*1024)
		h.mustSubmit(t, first, "new_byte_admitted", false, uniqueAccept(false, 1, 1))
		h.awaitWrites(t, 1)
		for i := 0; i < 2; i++ {
			h.mustSubmit(t, h.gen.next(t, 70*1024), "new_byte_rejected", false, uniqueReject(429, ErrCacheFull))
		}
		if n, b := h.n.cache.stats(); n != 1 || b != first.size || n >= h.n.cfg.CacheCount {
			t.Fatalf("byte pressure count=%d bytes=%d", n, b)
		}
	})
	t.Run("queue_rollback_partial_coverage_retry", func(t *testing.T) {
		h := newUniqueHarness(t, 256, defaults.CacheBytes, defaults.CacheTTL, report, t.Name())
		gate := make(chan struct{})
		slow := h.peer(t, 1, "blocked", gate)
		first := h.gen.next(t, 0)
		h.mustSubmit(t, first, "new_queue_fill", false, uniqueAccept(false, 1, 1))
		uniqueWait(t, "one in-flight blocked write", func() bool { return h.stats.get("write_attempts") == 1 })
		for i := 0; i < 64; i++ {
			h.mustSubmit(t, h.gen.next(t, 0), "new_queue_fill", false, uniqueAccept(false, 1, 1))
		}
		rejected := h.gen.next(t, 0)
		h.mustSubmit(t, rejected, "new_queue_rejected", false, uniqueReject(429, ErrQueuesFull))
		if n, _ := h.n.cache.stats(); n != 65 || !h.n.cache.expiry(rejected.hash).IsZero() {
			t.Fatal("fresh queue 429 retained cache entry")
		}
		h.sample(t)
		fast := h.peer(t, 2, "fast", nil)
		h.mustSubmit(t, first, "old_hash_retry_new_peer", true, uniqueAccept(true, 1, 1))
		partial := h.gen.next(t, 0)
		h.mustSubmit(t, partial, "new_partial_fanout", false, uniqueAccept(false, 1, 1))
		uniqueWait(t, "fast peer coverage", func() bool { return h.stats.get("fast_fixture_decoded") == 2 })
		h.mustSubmit(t, partial, "old_hash_retry_uncovered_full_peer", true, uniqueReject(429, ErrQueuesFull))
		if h.n.cache.expiry(partial.hash).IsZero() {
			t.Fatal("duplicate queue 429 removed prior accepted entry")
		}
		h.n.submitMu.Lock()
		_, fastCovered := fast.p.sent[partial.hash]
		_, slowCovered := slow.p.sent[partial.hash]
		h.n.submitMu.Unlock()
		if !fastCovered || slowCovered {
			t.Fatal("partial peer coverage mismatch")
		}
		close(gate)
		h.awaitWrites(t, 67)
		h.mustSubmit(t, partial, "old_hash_retry_remaining_peer", true, uniqueAccept(true, 1, 1))
		h.awaitWrites(t, 68)
		h.mustSubmit(t, partial, "old_hash_retry_all_covered", true, uniqueAccept(true, 0, 0))
		// The earlier rejected raw is still an old-hash retry in this experiment,
		// even though the relay correctly says duplicate=false after rollback.
		h.mustSubmit(t, rejected, "old_hash_retry_after_429_rollback", true, uniqueAccept(false, 2, 2))
		h.awaitWrites(t, 70)
		if h.stats.get("fast_fixture_decoded") != 3 || h.stats.get("blocked_fixture_decoded") != 67 {
			t.Fatal("unexpected final per-peer coverage")
		}
	})
	t.Run("short_ttl_expiry_and_retry", func(t *testing.T) {
		h := newUniqueHarness(t, 4, defaults.CacheBytes, 250*time.Millisecond, report, t.Name())
		p := h.peer(t, 1, "fast", nil)
		first := h.gen.next(t, 0)
		h.mustSubmit(t, first, "new_first_epoch", false, uniqueAccept(false, 1, 1))
		deadline := h.n.cache.expiry(first.hash)
		for i := 0; i < 3; i++ {
			f := h.gen.next(t, 0)
			h.mustSubmit(t, f, "new_first_epoch", false, uniqueAccept(false, 1, 1))
			deadline = h.n.cache.expiry(f.hash)
		}
		h.awaitWrites(t, 4)
		h.mustSubmit(t, h.gen.next(t, 0), "new_before_expiry_rejected", false, uniqueReject(429, ErrCacheFull))
		firstExpiry := h.n.cache.expiry(first.hash)
		h.mustSubmit(t, first, "old_hash_retry_before_expiry", true, uniqueAccept(true, 0, 0))
		if h.n.cache.expiry(first.hash) != firstExpiry {
			t.Fatal("duplicate extended TTL")
		}
		h.sample(t)
		time.Sleep(max(time.Until(deadline)+20*time.Millisecond, 0))
		h.n.prune(time.Now())
		if n, b := h.n.cache.stats(); n != 0 || b != 0 {
			t.Fatal("expired cache not reclaimed")
		}
		h.n.submitMu.Lock()
		sent := len(p.p.sent)
		h.n.submitMu.Unlock()
		if sent != 0 {
			t.Fatal("expired peer metadata not reclaimed")
		}
		h.sample(t)
		for i := 0; i < 3; i++ {
			h.mustSubmit(t, h.gen.next(t, 0), "new_after_expiry", false, uniqueAccept(false, 1, 1))
		}
		h.mustSubmit(t, first, "old_hash_retry_after_expiry", true, uniqueAccept(false, 1, 1))
		h.awaitWrites(t, 8)
	})
	t.Run("concurrent_unique", func(t *testing.T) {
		h := newUniqueHarness(t, 16, defaults.CacheBytes, defaults.CacheTTL, report, t.Name())
		h.peer(t, 1, "fast_a", nil)
		h.peer(t, 2, "fast_b", nil)
		contract := uniqueAccept(false, 2, 2)
		contract.rejections = map[int]string{429: ErrCacheFull.Error()}
		var wg sync.WaitGroup
		start := make(chan struct{})
		for worker := 0; worker < 4; worker++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				<-start
				for i := 0; i < 16; i++ {
					if _, _, err := h.submit(h.gen.next(t, 0), "new_concurrent", false, contract); err != nil {
						t.Error(err)
					}
					h.sample(t)
				}
			}()
		}
		close(start)
		wg.Wait()
		h.awaitWrites(t, 32)
		if h.counts.OfferedUnique != 64 || h.counts.Accepted != 16 || h.counts.Rejected429 != 48 || h.counts.Duplicate != 0 {
			t.Fatalf("concurrent counts: %+v", h.counts)
		}
		if n, _ := h.n.cache.stats(); n != 16 {
			t.Fatalf("concurrent occupancy=%d", n)
		}
	})
	t.Run("writer_failure_then_retry", func(t *testing.T) {
		h := newUniqueHarness(t, 4, defaults.CacheBytes, defaults.CacheTTL, report, t.Name())
		p := h.peer(t, 1, "failed", make(chan struct{}))
		f := h.gen.next(t, 0)
		h.mustSubmit(t, f, "new_before_writer_failure", false, uniqueAccept(false, 1, 1))
		uniqueWait(t, "blocked write before failure", func() bool { return h.stats.get("write_attempts") == 1 })
		p.close(t, h.n)
		if h.stats.get("write_failed") != 1 || h.n.cache.expiry(f.hash).IsZero() {
			t.Fatal("failure/cache outcome mismatch")
		}
		h.mustSubmit(t, h.gen.next(t, 0), "new_no_peers_after_failure", false, uniqueReject(503, ErrNoPeers))
		h.peer(t, 2, "replacement", nil)
		h.mustSubmit(t, f, "old_hash_retry_after_writer_failure", true, uniqueAccept(true, 1, 1))
		h.awaitWrites(t, 1)
	})
	t.Run("continuous_unique_short_ttl", func(t *testing.T) {
		h := newUniqueHarness(t, 4, defaults.CacheBytes, 250*time.Millisecond, report, t.Name())
		h.peer(t, 1, "fast", nil)
		uniqueStream(t, h, time.Second, 40)
		if h.counts.Accepted <= 4 || h.counts.Rejected429 == 0 || h.counts.Duplicate != 0 {
			t.Fatalf("stream did not prove expiry/rejection: %+v", h.counts)
		}
	})
}

// Closed-loop paced unique arrivals, no prefill or retries. The ticker does not
// accumulate a multi-tick catch-up queue, but at most one tick can remain pending:
// after a slow request, the next receive (and request) can be immediate. Dropped
// ticker opportunities are NOT offered requests. The report records both the
// configured pace and actual attempts over the actual measured interval.
func uniqueStream(t *testing.T, h *uniqueHarness, duration time.Duration, pace int) {
	t.Helper()
	contract := uniqueAccept(false, 1, 1)
	contract.rejections = map[int]string{429: ErrCacheFull.Error()}
	start := time.Now()
	end := start.Add(duration)
	ticker := time.NewTicker(time.Second / time.Duration(pace))
	defer ticker.Stop()
	var firstAccepted, lastAccepted time.Time
	var firstFull, firstRejected, lastRejectedBeforeRecovery, recovered, firstDeadline time.Time
	maxOffers := uint64(duration / (time.Second / time.Duration(pace)))
	for time.Now().Before(end) && h.counts.OfferedUnique < maxOffers {
		<-ticker.C
		if !time.Now().Before(end) {
			break
		}
		label := fmt.Sprintf("new_stream_second_%03d", int(time.Since(start).Seconds()))
		f := h.gen.next(t, 0)
		status, _ := h.mustSubmit(t, f, label, false, contract)
		now := time.Now()
		if status == 202 {
			if firstAccepted.IsZero() {
				firstAccepted = now
				firstDeadline = h.n.cache.expiry(f.hash)
			}
			lastAccepted = now
			if !firstRejected.IsZero() && recovered.IsZero() {
				recovered = now
			}
		} else {
			if firstRejected.IsZero() {
				firstRejected = now
			}
			if recovered.IsZero() {
				lastRejectedBeforeRecovery = now
			}
		}
		if firstFull.IsZero() && h.n.Health().CachedTransactions == h.n.cfg.CacheCount {
			firstFull = now
		}
		h.sample(t)
	}
	measured := time.Since(start)
	h.awaitWrites(t, h.counts.Accepted)
	if h.counts.Accepted == 0 || h.counts.Duplicate != 0 || h.counts.Retries != 0 {
		t.Fatal("fresh stream has no acceptance or contains retries/duplicates")
	}
	// This is a logic requirement, not a throughput threshold.
	if lastAccepted.Sub(firstAccepted) < h.n.cfg.CacheTTL {
		t.Fatal("stream did not observe admission beyond the first TTL")
	}
	if recovered.IsZero() || recovered.Before(firstDeadline) {
		t.Fatal("stream did not prove actual first-deadline recovery")
	}
	offset := func(at time.Time) any {
		if at.IsZero() {
			return nil
		}
		return at.Sub(start).Seconds()
	}
	// Keep measurement metadata independent of the cleanup-inclusive elapsed time.
	h.stream = map[string]any{
		"configured_pace_per_second": pace, "configured_duration_seconds": duration.Seconds(), "actual_interval_seconds": measured.Seconds(),
		"offered_unique_hashes": h.counts.OfferedUnique, "accepted_unique_hashes": h.counts.Accepted, "old_hash_retries": h.counts.Retries,
		"actual_offered_unique_per_second":              float64(h.counts.OfferedUnique) / measured.Seconds(),
		"accepted_unique_per_second_over_finite_window": float64(h.counts.Accepted) / measured.Seconds(),
		"first_to_last_accepted_seconds":                lastAccepted.Sub(firstAccepted).Seconds(),
		"maximum_offers":                                maxOffers, "stream_start_ns_since_scenario_start": start.Sub(h.start).Nanoseconds(),
		"first_full_cache_observed_seconds": offset(firstFull), "first_cache_429_seconds": offset(firstRejected),
		"last_cache_429_before_first_recovery_seconds": offset(lastRejectedBeforeRecovery),
		"first_cache_deadline_seconds":                 offset(firstDeadline), "first_acceptance_after_cache_429_seconds": offset(recovered),
		"interpretation": "finite closed-loop window with startup capacity; not a steady-state or production throughput estimate",
	}
	t.Logf("UNIQUE_STREAM offered=%d accepted=%d rejected429=%d duplicates=%d retries=%d measured_seconds=%.6f actual_offered_per_second=%.3f accepted_per_second_over_finite_window=%.3f configured_pace=%d TTL=%s (not steady-state production TPS)", h.counts.OfferedUnique, h.counts.Accepted, h.counts.Rejected429, h.counts.Duplicate, h.counts.Retries, measured.Seconds(), float64(h.counts.OfferedUnique)/measured.Seconds(), float64(h.counts.Accepted)/measured.Seconds(), pace, h.n.cfg.CacheTTL)
}

func TestUniqueCapacityDefaultTTL(t *testing.T) {
	if os.Getenv("RELAY_UNIQUE_DEFAULTS") != "1" {
		t.Skip("opt-in 150s default-limit experiment; see docs/UNIQUE_CAPACITY.md")
	}
	if runtime.GOMAXPROCS(0) > 2 {
		t.Fatal("default-TTL experiment requires GOMAXPROCS<=2")
	}
	report := uniqueReport(t, "opt-in finite 150s stream; default cache limits and 120s TTL; not steady-state or production throughput")
	t.Run("default_120s_ttl", func(t *testing.T) {
		cfg := DefaultConfig()
		if cfg.CacheCount != 1024 || cfg.CacheBytes != 16*1024*1024 || cfg.CacheTTL != 120*time.Second {
			t.Fatal("default configuration changed; explicitly review the experiment design")
		}
		h := newUniqueHarness(t, cfg.CacheCount, cfg.CacheBytes, cfg.CacheTTL, report, t.Name())
		h.peer(t, 1, "fast", nil)
		uniqueStream(t, h, 150*time.Second, 20)
		if h.counts.Accepted <= uint64(cfg.CacheCount) || h.counts.Rejected429 == 0 {
			t.Fatal("default experiment did not observe capacity rejection and recovery")
		}
	})
}
