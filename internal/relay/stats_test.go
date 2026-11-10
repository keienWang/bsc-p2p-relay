// SPDX-License-Identifier: GPL-3.0-or-later
package relay

import (
	"bytes"
	cryptorand "crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"math"
	"net/http"
	"net/http/httptest"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/p2p/enode"
)

func testStatsIdentity(tb testing.TB) statsIdentity {
	tb.Helper()
	identity, err := newStatsIdentity(cryptorand.Reader)
	if err != nil {
		tb.Fatal(err)
	}
	return identity
}

func statsRequest(h http.Handler, method, path, body string) *httptest.ResponseRecorder {
	r := httptest.NewRequest(method, "http://localhost"+path, strings.NewReader(body))
	r.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	return w
}

var statsSubmitFields = []string{
	"accepted_new_cache_lifetime_total", "accepted_cached_retry_total", "rejected_validation_total",
	"rejected_no_peers_total", "rejected_cache_full_total", "rejected_queue_unavailable_total",
	"rejected_admission_expired_total", "other_total",
}

func strictStats(t *testing.T, w *httptest.ResponseRecorder) Stats {
	t.Helper()
	if w.Code != 200 || w.Header().Get("Content-Type") != "application/json" || w.Header().Get("Cache-Control") != "no-store" || w.Header().Get("X-Content-Type-Options") != "nosniff" {
		t.Fatalf("invalid stats status/headers: %d %v", w.Code, w.Header())
	}
	fields, err := soakStrictObject(w.Body.Bytes(), "schema_version", "instance_id", "created_at", "observed_at", "submit", "http")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := soakStrictObject(fields["submit"], statsSubmitFields...); err != nil {
		t.Fatal(err)
	}
	if _, err := soakStrictObject(fields["http"], "concurrency_rejected_total"); err != nil {
		t.Fatal(err)
	}
	var got Stats
	if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if got.SchemaVersion != 1 || !regexp.MustCompile(`^[0-9a-f]{32}$`).MatchString(got.InstanceID) || got.InstanceID == strings.Repeat("0", 32) {
		t.Fatalf("invalid version/identity: %+v", got)
	}
	for _, stamp := range []time.Time{got.CreatedAt, got.ObservedAt} {
		if stamp.IsZero() || stamp.Location() != time.UTC {
			t.Fatalf("invalid UTC metadata: %v", stamp)
		}
	}
	// No observed >= created assertion: descriptive wall clocks can move backward.
	return got
}

func assertStatsOutcomes(t *testing.T, n *Node, want map[submitOutcome]uint64) {
	t.Helper()
	for outcome := submitOther; outcome < submitOutcomeCount; outcome++ {
		if got := n.stats.outcomes[outcome].Load(); got != want[outcome] {
			t.Fatalf("outcome %d = %d, want %d", outcome, got, want[outcome])
		}
	}
}

func TestStatsIdentityAndSharedHandlers(t *testing.T) {
	cfg := DefaultConfig()
	cfg.NoDiscovery, cfg.ListenAddr = true, "127.0.0.1:0"
	a, err := New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	b, err := New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	// Constructors only: no P2P start, socket, discovery or external connection.
	h1, h2 := a.Handler(), a.Handler()
	first := strictStats(t, statsRequest(h1, "GET", "/v1/stats", ""))
	other := strictStats(t, statsRequest(b.Handler(), "GET", "/v1/stats", ""))
	if first.InstanceID == other.InstanceID {
		t.Fatal("new Node reused instance identity")
	}
	for _, h := range []http.Handler{h1, h2} {
		if w := statsRequest(h, "POST", "/v1/transactions", `{"raw_tx":"0x00"}`); w.Code != 400 {
			t.Fatal(w.Code)
		}
	}
	for _, h := range []http.Handler{h1, h2, a.Handler()} {
		got := strictStats(t, statsRequest(h, "GET", "/v1/stats", ""))
		if got.InstanceID != first.InstanceID || !got.CreatedAt.Equal(first.CreatedAt) || got.Submit.RejectedValidationTotal != 2 {
			t.Fatal("handler changed identity/epoch or reset counters")
		}
	}
	assertStatsOutcomes(t, b, nil)
}

type statsEntropyError struct{ err error }

func (r statsEntropyError) Read([]byte) (int, error) { return 0, r.err }

func TestStatsIdentityEntropyFailures(t *testing.T) {
	failure := errors.New("fixture entropy unavailable")
	for _, source := range []io.Reader{statsEntropyError{failure}, bytes.NewReader(make([]byte, 15))} {
		identity, err := newStatsIdentity(source)
		if err == nil || identity != (statsIdentity{}) {
			t.Fatal("entropy failure produced fallback identity")
		}
	}
	if _, err := newStatsIdentity(statsEntropyError{failure}); !errors.Is(err, failure) {
		t.Fatal("entropy cause lost")
	}
}

func TestStatsSchemaGuardsAndLegacyShapes(t *testing.T) {
	n := bareNode(t)
	h := n.Handler()
	strictStats(t, statsRequest(h, "GET", "/v1/stats", ""))
	for _, tc := range []struct {
		method, host, origin string
		status               int
	}{
		{"POST", "localhost", "", 405}, {"GET", "evil.example", "", 403},
		{"GET", "localhost", "https://example.com", 403},
	} {
		r := httptest.NewRequest(tc.method, "http://localhost/v1/stats", nil)
		r.Host = tc.host
		r.Header.Set("Origin", tc.origin)
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		if w.Code != tc.status {
			t.Fatalf("guard %v: %d", tc, w.Code)
		}
		if _, err := soakStrictObject(w.Body.Bytes(), "error"); err != nil {
			t.Fatal(err)
		}
	}
	for _, path := range []string{"/healthz", "/readyz"} {
		w := statsRequest(h, "GET", path, "")
		if w.Code != map[string]int{"/healthz": 200, "/readyz": 503}[path] {
			t.Fatal(w.Code)
		}
		if _, err := soakStrictObject(w.Body.Bytes(), "network", "chain_id", "ready_peers", "connected_peers", "cached_transactions", "cached_bytes", "written_messages", "write_failures", "confirmation_tracking"); err != nil {
			t.Fatal(err)
		}
	}
	assertStatsOutcomes(t, n, nil)
	raw := rawFixture(t, fixtureTx(t, 56, 0, false))
	addFakePeer(n, true, 1)
	w := statsRequest(h, "POST", "/v1/transactions", `{"raw_tx":"0x`+hex.EncodeToString(raw)+`"}`)
	if w.Code != 202 {
		t.Fatal(w.Code, w.Body.String())
	}
	if _, err := soakStrictObject(w.Body.Bytes(), "hash", "sender", "queued_peers", "duplicate", "stage"); err != nil {
		t.Fatal(err)
	}
	before := n.Stats().Submit
	w = statsRequest(h, "GET", "/v1/stats", "")
	strictStats(t, w)
	if n.Stats().Submit != before {
		t.Fatal("statistics call changed Submit counters")
	}
	for _, forbidden := range []string{hex.EncodeToString(raw), `"hash"`, `"sender"`, `"peer"`, `"enode"`, `"raw_tx"`} {
		if strings.Contains(w.Body.String(), forbidden) {
			t.Fatalf("stats leaked %q", forbidden)
		}
	}
}

func TestStatsUInt64JSONAndOtherClassification(t *testing.T) {
	n := bareNode(t)
	n.stats.record(submitOther)
	n.stats.record(submitOutcome(255))
	assertStatsOutcomes(t, n, map[submitOutcome]uint64{submitOther: 2})
	for _, value := range []uint64{1<<53 + 1, math.MaxUint64} {
		for i := range n.stats.outcomes {
			n.stats.outcomes[i].Store(value)
		}
		n.stats.concurrencyRejected.Store(value)
		w := statsRequest(n.Handler(), "GET", "/v1/stats", "")
		strictStats(t, w)
		decoder := json.NewDecoder(bytes.NewReader(w.Body.Bytes()))
		decoder.UseNumber()
		var object map[string]any
		if err := decoder.Decode(&object); err != nil {
			t.Fatal(err)
		}
		for _, group := range []string{"submit", "http"} {
			for key, raw := range object[group].(map[string]any) {
				number, ok := raw.(json.Number)
				if !ok {
					t.Fatalf("%s is not a number", key)
				}
				got, err := strconv.ParseUint(number.String(), 10, 64)
				if err != nil || got != value {
					t.Fatalf("%s lost uint64 precision: %v %v", key, number, err)
				}
			}
		}
	}
}

func TestStatsDistinctHTTPFieldMapping(t *testing.T) {
	n := bareNode(t)
	cases := []struct {
		outcome submitOutcome
		field   string
		value   uint64
	}{
		{submitAcceptedNew, "accepted_new_cache_lifetime_total", 11},
		{submitAcceptedCached, "accepted_cached_retry_total", 22},
		{submitRejectedValidation, "rejected_validation_total", 33},
		{submitRejectedNoPeers, "rejected_no_peers_total", 44},
		{submitRejectedCacheFull, "rejected_cache_full_total", 55},
		{submitRejectedQueueUnavailable, "rejected_queue_unavailable_total", 66},
		{submitRejectedAdmissionExpired, "rejected_admission_expired_total", 77},
		{submitOther, "other_total", 88},
	}
	for _, tc := range cases {
		n.stats.outcomes[tc.outcome].Store(tc.value)
	}
	n.stats.concurrencyRejected.Store(99)
	w := statsRequest(n.Handler(), "GET", "/v1/stats", "")
	strictStats(t, w)
	var got struct {
		Submit map[string]uint64 `json:"submit"`
		HTTP   map[string]uint64 `json:"http"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	for _, tc := range cases {
		if value, ok := got.Submit[tc.field]; !ok || value != tc.value {
			t.Fatalf("counter %d mapped to %s=%d, want %d", tc.outcome, tc.field, value, tc.value)
		}
	}
	if got.HTTP["concurrency_rejected_total"] != 99 {
		t.Fatal("HTTP rejection counter mapping changed")
	}
}

func TestStatsSubmitOutcomePaths(t *testing.T) {
	raw := rawFixture(t, fixtureTx(t, 56, 0, false))
	for _, tc := range []struct {
		name  string
		setup func(*Node)
		raw   []byte
		want  submitOutcome
		err   error
	}{
		{"validation_before_no_peers", func(*Node) {}, nil, submitRejectedValidation, nil},
		{"no_peers", func(*Node) {}, raw, submitRejectedNoPeers, ErrNoPeers},
		{"opt_out", func(n *Node) { addFakePeer(n, false, 1) }, raw, submitRejectedNoPeers, ErrNoPeers},
		{"queue_full", func(n *Node) { addFakePeer(n, true, 0) }, raw, submitRejectedQueueUnavailable, ErrQueuesFull},
		{"closing_session", func(n *Node) { close(addFakePeer(n, true, 1).done) }, raw, submitRejectedQueueUnavailable, ErrQueuesFull},
		{"sent_limit", func(n *Node) {
			p := addFakePeer(n, true, 1)
			n.cfg.CacheCount = 1
			p.sent = map[common.Hash]time.Time{{1}: time.Now().Add(time.Hour)}
		}, raw, submitRejectedQueueUnavailable, ErrQueuesFull},
		{"byte_limit", func(n *Node) { addFakePeer(n, true, 1); n.cache = newStore(2, 1, time.Minute) }, raw, submitRejectedCacheFull, ErrCacheFull},
		{"count_limit", func(n *Node) { addFakePeer(n, true, 1); n.cache = newStore(0, MaxRawTransaction, time.Minute) }, raw, submitRejectedCacheFull, ErrCacheFull},
		// Invalid internal TTL is a deterministic branch fault, not allowed by New.
		{"admission_expired", func(n *Node) { addFakePeer(n, true, 1); n.cache = newStore(2, MaxRawTransaction, -time.Minute) }, raw, submitRejectedAdmissionExpired, ErrAdmissionExpired},
	} {
		t.Run(tc.name, func(t *testing.T) {
			n := bareNode(t)
			tc.setup(n)
			_, err := n.Submit(tc.raw)
			if err == nil || tc.err != nil && !errors.Is(err, tc.err) {
				t.Fatalf("unexpected error: %v", err)
			}
			assertStatsOutcomes(t, n, map[submitOutcome]uint64{tc.want: 1})
			if n.Stats().HTTP.ConcurrencyRejectedTotal != 0 {
				t.Fatal("direct Submit changed HTTP counter")
			}
			if count, _ := n.cache.stats(); count != 0 {
				t.Fatal("rejection retained a live fresh entry")
			}
		})
	}
}

func TestStatsPartialFanoutRollbackRetryAndTTL(t *testing.T) {
	n := bareNode(t)
	tx := fixtureTx(t, 56, 0, false)
	raw := rawFixture(t, tx)
	p := addFakePeer(n, true, 1)
	n.peers[enode.ID{2}] = &peerSession{receiveTx: true, queue: make(chan outbound), done: make(chan struct{})}
	got, err := n.Submit(raw)
	if err != nil || got.Duplicate || got.QueuedPeers != 1 {
		t.Fatal(got, err)
	}
	deadline := n.cache.expiry(tx.Hash())
	if _, err = n.Submit(raw); !errors.Is(err, ErrQueuesFull) {
		t.Fatal(err)
	}
	if n.cache.expiry(tx.Hash()) != deadline {
		t.Fatal("cached rejection changed retention")
	}
	n.peers[enode.ID{2}] = &peerSession{receiveTx: true, queue: make(chan outbound, 1), done: make(chan struct{})}
	got, err = n.Submit(raw)
	if err != nil || !got.Duplicate || got.QueuedPeers != 1 {
		t.Fatal(got, err)
	}
	got, err = n.Submit(raw)
	if err != nil || !got.Duplicate || got.QueuedPeers != 0 || got.Stage != "cached" {
		t.Fatal(got, err)
	}
	if n.cache.expiry(tx.Hash()) != deadline {
		t.Fatal("duplicate extended deadline")
	}
	assertStatsOutcomes(t, n, map[submitOutcome]uint64{submitAcceptedNew: 1, submitAcceptedCached: 2, submitRejectedQueueUnavailable: 1})
	// Expire both records, drain old queue items, then the SAME hash is new again.
	expired := time.Now().Add(-time.Second)
	setCacheExpiryForTest(t, n.cache, expired, tx.Hash())
	for _, peer := range n.peers {
		peer.sent[tx.Hash()] = expired
		<-peer.queue
	}
	got, err = n.Submit(raw)
	if err != nil || got.Duplicate || got.QueuedPeers != 2 {
		t.Fatal(got, err)
	}
	assertStatsOutcomes(t, n, map[submitOutcome]uint64{submitAcceptedNew: 2, submitAcceptedCached: 2, submitRejectedQueueUnavailable: 1})
	// A different fresh hash fails all full queues, rolls back, then retries fresh.
	other := fixtureTx(t, 56, 1, false)
	otherRaw := rawFixture(t, other)
	if _, err = n.Submit(otherRaw); !errors.Is(err, ErrQueuesFull) {
		t.Fatal(err)
	}
	if !n.cache.expiry(other.Hash()).IsZero() {
		t.Fatal("fresh failed fanout retained insertion")
	}
	<-p.queue
	got, err = n.Submit(otherRaw)
	if err != nil || got.Duplicate || got.QueuedPeers != 1 {
		t.Fatal(got, err)
	}
	assertStatsOutcomes(t, n, map[submitOutcome]uint64{submitAcceptedNew: 3, submitAcceptedCached: 2, submitRejectedQueueUnavailable: 2})
}

func TestStatsNoCompletionOnPanicOrBlockedCall(t *testing.T) {
	raw := rawFixture(t, fixtureTx(t, 56, 0, false))
	n := bareNode(t)
	addFakePeer(n, true, 1)
	n.cache = nil
	var recovered any
	func() { defer func() { recovered = recover() }(); _, _ = n.Submit(raw) }()
	if recovered == nil {
		t.Fatal("malformed internal fixture did not propagate panic")
	}
	assertStatsOutcomes(t, n, nil)
	n = bareNode(t)
	n.submitMu.Lock()
	var unlock sync.Once
	defer unlock.Do(n.submitMu.Unlock)
	started, done := make(chan struct{}), make(chan error, 1)
	go func() { close(started); _, err := n.Submit(raw); done <- err }()
	<-started
	assertStatsOutcomes(t, n, nil)
	strictStats(t, statsRequest(n.Handler(), "GET", "/v1/stats", ""))
	unlock.Do(n.submitMu.Unlock)
	select {
	case err := <-done:
		if !errors.Is(err, ErrNoPeers) {
			t.Fatal(err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("Submit did not finish")
	}
	assertStatsOutcomes(t, n, map[submitOutcome]uint64{submitRejectedNoPeers: 1})
}

func TestStatsSnapshotNeedsNoStateLocks(t *testing.T) {
	n := bareNode(t)
	// All three locks held in the existing order: stats must still finish.
	n.submitMu.Lock()
	n.mu.Lock()
	n.cache.mu.Lock()
	defer n.submitMu.Unlock()
	defer n.mu.Unlock()
	defer n.cache.mu.Unlock()
	done := make(chan *httptest.ResponseRecorder, 1)
	go func() { done <- statsRequest(n.Handler(), "GET", "/v1/stats", "") }()
	select {
	case w := <-done:
		strictStats(t, w)
	case <-time.After(3 * time.Second):
		t.Fatal("stats took a state lock")
	}
}

type statsBlockedBody struct {
	entered chan<- struct{}
	release <-chan struct{}
	once    sync.Once
	reader  io.Reader
}

func (b *statsBlockedBody) Read(p []byte) (int, error) {
	b.once.Do(func() { b.entered <- struct{}{}; <-b.release })
	return b.reader.Read(p)
}
func (*statsBlockedBody) Close() error { return nil }

type statsFailedResponseWriter struct {
	header http.Header
	status int
	writes int
}

func (w *statsFailedResponseWriter) Header() http.Header    { return w.header }
func (w *statsFailedResponseWriter) WriteHeader(status int) { w.status = status }
func (w *statsFailedResponseWriter) Write([]byte) (int, error) {
	w.writes++
	return 0, errors.New("fixture response write failed")
}

func TestStatsHTTPConcurrencyAndHandlerIsolation(t *testing.T) {
	n := bareNode(t)
	h1, h2 := n.Handler(), n.Handler()
	entered, release, done := make(chan struct{}, 8), make(chan struct{}), make(chan int, 8)
	var unblock sync.Once
	defer unblock.Do(func() { close(release) })
	for i := 0; i < 8; i++ {
		go func() {
			r := httptest.NewRequest("POST", "http://localhost/v1/transactions", nil)
			r.Header.Set("Content-Type", "application/json")
			r.Body = &statsBlockedBody{entered: entered, release: release, reader: strings.NewReader(`{"raw_tx":"0x00"}`)}
			w := httptest.NewRecorder()
			h1.ServeHTTP(w, r)
			done <- w.Code
		}()
	}
	for i := 0; i < 8; i++ {
		select {
		case <-entered:
		case <-time.After(3 * time.Second):
			t.Fatal("eight slots not acquired")
		}
	}
	w := statsRequest(h1, "POST", "/v1/transactions", `{"raw_tx":"0x00"}`)
	if w.Code != 429 || !strings.Contains(w.Body.String(), "too many concurrent requests") {
		t.Fatal(w.Code, w.Body.String())
	}
	assertStatsOutcomes(t, n, nil)
	// The same full-slot decision counts even when its response cannot be written.
	failed := &statsFailedResponseWriter{header: make(http.Header)}
	request := httptest.NewRequest("POST", "http://localhost/v1/transactions", strings.NewReader(`{"raw_tx":"0x00"}`))
	request.Header.Set("Content-Type", "application/json")
	h1.ServeHTTP(failed, request)
	if failed.status != 429 || failed.writes != 1 {
		t.Fatalf("failed writer did not exercise the 429 response: %+v", failed)
	}
	assertStatsOutcomes(t, n, nil)
	first := strictStats(t, statsRequest(h1, "GET", "/v1/stats", ""))
	if first.HTTP.ConcurrencyRejectedTotal != 2 {
		t.Fatal(first.HTTP)
	}
	if w = statsRequest(h2, "POST", "/v1/transactions", `{"raw_tx":"0x00"}`); w.Code != 400 {
		t.Fatal("Handlers incorrectly share slots", w.Code)
	}
	second := strictStats(t, statsRequest(h2, "GET", "/v1/stats", ""))
	if second.InstanceID != first.InstanceID || second.HTTP.ConcurrencyRejectedTotal != 2 || second.Submit.RejectedValidationTotal != 1 {
		t.Fatal("Handlers do not share counters/identity")
	}
	unblock.Do(func() { close(release) })
	for i := 0; i < 8; i++ {
		select {
		case code := <-done:
			if code != 400 {
				t.Fatal(code)
			}
		case <-time.After(3 * time.Second):
			t.Fatal("slots did not recover")
		}
	}
	if w = statsRequest(h1, "POST", "/v1/transactions", `{"raw_tx":"0x00"}`); w.Code != 400 {
		t.Fatal(w.Code)
	}
	assertStatsOutcomes(t, n, map[submitOutcome]uint64{submitRejectedValidation: 10})
}

func TestStatsEarlyHTTPRejectionsHaveNoSubmitOutcome(t *testing.T) {
	n := bareNode(t)
	h := n.Handler()
	for _, body := range []string{`{`, `{} {}`, `{"unexpected":1}`, `{"raw_tx":"00"}`, `{"raw_tx":"0xqq"}`} {
		if w := statsRequest(h, "POST", "/v1/transactions", body); w.Code != 400 {
			t.Fatal(w.Code)
		}
	}
	for _, tc := range []struct {
		method, host, origin, media string
		status                      int
	}{
		{"GET", "localhost", "", "application/json", 405},
		{"POST", "evil.example", "", "application/json", 403},
		{"POST", "localhost", "https://example.com", "application/json", 403},
		{"POST", "localhost", "", "text/plain", 415},
	} {
		r := httptest.NewRequest(tc.method, "http://localhost/v1/transactions", strings.NewReader(`{"raw_tx":"0x00"}`))
		r.Host = tc.host
		r.Header.Set("Origin", tc.origin)
		r.Header.Set("Content-Type", tc.media)
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		if w.Code != tc.status {
			t.Fatal(w.Code)
		}
	}
	assertStatsOutcomes(t, n, nil)
	if n.Stats().HTTP.ConcurrencyRejectedTotal != 0 {
		t.Fatal("misclassified early rejection")
	}
}

func TestStatsConcurrentSnapshotsAndSubmissions(t *testing.T) {
	n := loadNode(t, 64, 2, 64)
	_, raws := loadFixtures(t, 16)
	var wg sync.WaitGroup
	for worker := 0; worker < 8; worker++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for _, raw := range raws {
				if _, err := n.Submit(raw); err != nil {
					t.Error(err)
				}
			}
		}()
	}
	wg.Add(1)
	go func() {
		defer wg.Done()
		for i := 0; i < 128; i++ {
			n.Stats()
			n.Health()
			n.prune(time.Now())
		}
	}()
	wg.Wait()
	assertStatsOutcomes(t, n, map[submitOutcome]uint64{submitAcceptedNew: 16, submitAcceptedCached: 112})
	assertLoadBounds(t, n)
}
