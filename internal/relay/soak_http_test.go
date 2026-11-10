// SPDX-License-Identifier: GPL-3.0-or-later
package relay

import (
	"bytes"
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/big"
	"mime"
	"net"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/types"
)

// Each call declares its expected phase before the request. 503 is allowed
// only for the explicit probe after ALL sessions have stopped, never during
// active reconnects: the slow peer remains available throughout those changes.
type soakHTTPPhase string

const (
	soakHTTPPrefill      soakHTTPPhase = "prefill"
	soakHTTPAdmission    soakHTTPPhase = "expiry_admission"
	soakHTTPActive       soakHTTPPhase = "active_load"
	soakHTTPCacheFull    soakHTTPPhase = "cache_full_probe"
	soakHTTPQueueFull    soakHTTPPhase = "queue_full_probe"
	soakHTTPDisconnected soakHTTPPhase = "disconnected_probe"
)

func (p soakHTTPPhase) measured() bool { return p != soakHTTPPrefill }

type soakRequest struct {
	body   []byte
	hash   common.Hash
	sender common.Address
}

func soakBodies(t *testing.T, count int) []soakRequest {
	t.Helper()
	txs, raws := loadFixtures(t, count)
	requests := make([]soakRequest, len(raws))
	for i, raw := range raws {
		sender, err := types.Sender(types.LatestSignerForChainID(big.NewInt(56)), txs[i])
		if err != nil {
			t.Fatal(err)
		}
		requests[i] = soakRequest{[]byte(`{"raw_tx":"0x` + hex.EncodeToString(raw) + `"}`), txs[i].Hash(), sender}
	}
	return requests
}

type soakAPI struct {
	server *httptest.Server
	client *http.Client
	stats  *soakStats
}

func newSoakAPI(t *testing.T, n *Node, stats *soakStats) *soakAPI {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	handler := n.Handler()
	s := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		handler.ServeHTTP(w, r)
		stats.observe("http_handler_admission_and_response", time.Since(start))
	}))
	s.Listener.Close()
	s.Listener = listener
	s.Start()
	transport := &http.Transport{Proxy: nil, MaxIdleConns: 32, MaxIdleConnsPerHost: 32, MaxConnsPerHost: 32}
	client := &http.Client{Transport: transport, Timeout: 2 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	t.Cleanup(func() { client.CloseIdleConnections(); s.Close() })
	return &soakAPI{s, client, stats}
}
func (a *soakAPI) httpCount(phase soakHTTPPhase, key string) {
	a.stats.count("all_http_"+key, 1)
	if phase.measured() {
		a.stats.count("http_"+key, 1)
	}
}
func (a *soakAPI) submit(ctx context.Context, fixture soakRequest, phase soakHTTPPhase) (int, Submission, error) {
	a.httpCount(phase, "attempts")
	a.stats.count("phase_"+string(phase)+"_attempts", 1)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, a.server.URL+"/v1/transactions", bytes.NewReader(fixture.body))
	if err != nil {
		a.httpCount(phase, "client_error")
		a.stats.httpFailure(err)
		return 0, Submission{}, err
	}
	req.Header.Set("Content-Type", "application/json")
	start := time.Now()
	resp, err := a.client.Do(req)
	if err != nil {
		a.httpCount(phase, "client_error")
		failure := fmt.Errorf("%s client: %w", phase, err)
		a.stats.httpFailure(failure)
		return 0, Submission{}, failure
	}
	a.httpCount(phase, "requests")
	a.httpCount(phase, strconv.Itoa(resp.StatusCode))
	// Limit+1 distinguishes a complete bounded response from a silently truncated
	// prefix. A read/close error is one invalid response, even if JSON looks valid.
	payload, readErr := io.ReadAll(io.LimitReader(resp.Body, 8193))
	closeErr := resp.Body.Close()
	elapsed := time.Since(start)
	if phase.measured() {
		a.stats.observe("http_roundtrip_all", elapsed)
		a.stats.observe("http_roundtrip_"+strconv.Itoa(resp.StatusCode), elapsed)
	}
	invalid := func(reason string, cause error) (int, Submission, error) {
		a.httpCount(phase, "invalid_responses")
		a.httpCount(phase, reason)
		failure := fmt.Errorf("%s HTTP %d %s: %w", phase, resp.StatusCode, reason, cause)
		a.stats.httpFailure(failure)
		return resp.StatusCode, Submission{}, failure
	}
	if readErr != nil || closeErr != nil {
		return invalid("read_error", errors.Join(readErr, closeErr))
	}
	if len(payload) > 8192 {
		return invalid("body_error", errors.New("response exceeds 8192 bytes"))
	}
	sub, rejection, reason, err := soakValidateHTTP(resp.StatusCode, resp.Header.Get("Content-Type"), payload, fixture, phase)
	if err != nil {
		return invalid(reason, err)
	}
	a.httpCount(phase, "valid_responses")
	a.httpCount(phase, "valid_"+strconv.Itoa(resp.StatusCode))
	if resp.StatusCode == 202 {
		a.stats.count("queue_admissions", uint64(sub.QueuedPeers))
		if phase.measured() && sub.QueuedPeers == 0 {
			a.stats.count("accepted_without_new_queue", 1)
		}
	} else {
		a.stats.count("all_rejection_"+rejection, 1)
		if phase.measured() {
			a.stats.count("rejection_"+rejection, 1)
		}
	}
	return resp.StatusCode, sub, nil
}

// Strict object decoding rejects missing/extra/duplicate/null fields and trailing
// JSON. In particular {} must never become an all-zero, apparently valid 202.
func soakStrictObject(payload []byte, fields ...string) (map[string]json.RawMessage, error) {
	decoder := json.NewDecoder(bytes.NewReader(payload))
	token, err := decoder.Token()
	if err != nil {
		return nil, err
	}
	if token != json.Delim('{') {
		return nil, errors.New("expected JSON object")
	}
	allowed := map[string]bool{}
	for _, field := range fields {
		allowed[field] = true
	}
	values := map[string]json.RawMessage{}
	for decoder.More() {
		token, err = decoder.Token()
		if err != nil {
			return nil, err
		}
		key, ok := token.(string)
		if !ok {
			return nil, errors.New("non-string object key")
		}
		if !allowed[key] {
			return nil, fmt.Errorf("unknown field %q", key)
		}
		if _, exists := values[key]; exists {
			return nil, fmt.Errorf("duplicate field %q", key)
		}
		var value json.RawMessage
		if err = decoder.Decode(&value); err != nil {
			return nil, err
		}
		if bytes.Equal(bytes.TrimSpace(value), []byte("null")) {
			return nil, fmt.Errorf("null field %q", key)
		}
		values[key] = value
	}
	if _, err = decoder.Token(); err != nil {
		return nil, err
	}
	var extra any
	if err = decoder.Decode(&extra); err != io.EOF {
		return nil, errors.New("trailing JSON/content")
	}
	if len(values) != len(fields) {
		return nil, errors.New("missing required response field")
	}
	return values, nil
}
func soakValidateHTTP(status int, contentType string, payload []byte, fixture soakRequest, phase soakHTTPPhase) (Submission, string, string, error) {
	fail := func(reason string, err error) (Submission, string, string, error) {
		return Submission{}, "", reason, err
	}
	media, _, err := mime.ParseMediaType(contentType)
	if err != nil || media != "application/json" {
		return fail("header_error", errors.New("expected application/json response"))
	}
	// Unknown phases also fail closed rather than implicitly authorizing statuses.
	switch phase {
	case soakHTTPPrefill, soakHTTPAdmission, soakHTTPActive, soakHTTPCacheFull, soakHTTPQueueFull, soakHTTPDisconnected:
	default:
		return fail("semantic_error", fmt.Errorf("unknown request phase %q", phase))
	}
	allowedStatus := status == 202 && (phase == soakHTTPPrefill || phase == soakHTTPAdmission || phase == soakHTTPActive) || status == 429 && (phase == soakHTTPActive || phase == soakHTTPCacheFull || phase == soakHTTPQueueFull) || status == 503 && phase == soakHTTPDisconnected
	if !allowedStatus {
		return fail("status_error", fmt.Errorf("unexpected status %d in %s", status, phase))
	}
	if !json.Valid(payload) {
		return fail("decode_error", errors.New("invalid response JSON"))
	}
	if status == 202 {
		fields, err := soakStrictObject(payload, "hash", "sender", "queued_peers", "duplicate", "stage")
		if err != nil {
			return fail("schema_error", err)
		}
		var sub Submission
		for _, field := range []struct {
			key   string
			value any
		}{{"hash", &sub.Hash}, {"sender", &sub.Sender}, {"queued_peers", &sub.QueuedPeers}, {"duplicate", &sub.Duplicate}, {"stage", &sub.Stage}} {
			if err = json.Unmarshal(fields[field.key], field.value); err != nil {
				return fail("schema_error", fmt.Errorf("%s: %w", field.key, err))
			}
		}
		if sub.Hash != fixture.hash || sub.Sender != fixture.sender {
			return fail("semantic_error", errors.New("response hash/sender differs from signed fixture"))
		}
		maxPeers := 1
		if phase == soakHTTPActive {
			maxPeers = 3
		}
		if sub.QueuedPeers < 0 || sub.QueuedPeers > maxPeers {
			return fail("semantic_error", fmt.Errorf("queued_peers %d outside 0..%d", sub.QueuedPeers, maxPeers))
		}
		if sub.QueuedPeers == 0 {
			if sub.Stage != "cached" || !sub.Duplicate {
				return fail("semantic_error", errors.New("zero admissions require cached duplicate"))
			}
		} else if sub.Stage != "queued" {
			return fail("semantic_error", errors.New("positive admissions require queued stage"))
		}
		if phase == soakHTTPPrefill || phase == soakHTTPAdmission {
			if sub.QueuedPeers != 1 || sub.Duplicate {
				return fail("semantic_error", errors.New("fresh single-peer fixture requires one admission and duplicate=false"))
			}
		}
		return sub, "", "", nil
	}
	fields, err := soakStrictObject(payload, "error")
	if err != nil {
		return fail("schema_error", err)
	}
	var message string
	if err = json.Unmarshal(fields["error"], &message); err != nil {
		return fail("schema_error", err)
	}
	expected := false
	switch phase {
	case soakHTTPActive:
		expected = status == 429 && (message == ErrQueuesFull.Error() || message == ErrAdmissionExpired.Error() || message == "too many concurrent requests")
	case soakHTTPCacheFull:
		expected = status == 429 && message == ErrCacheFull.Error()
	case soakHTTPQueueFull:
		expected = status == 429 && message == ErrQueuesFull.Error()
	case soakHTTPDisconnected:
		expected = status == 503 && message == ErrNoPeers.Error()
	}
	if !expected {
		return fail("semantic_error", fmt.Errorf("unexpected error %q for %s HTTP %d", message, phase, status))
	}
	return Submission{}, message, "", nil
}

// Used by BOTH measured scenarios and expiry. Error categories are exclusive
// per response, so their denominators can be checked without double counting.
func soakValidateScenario(stats *soakStats) error {
	stats.mu.Lock()
	defer stats.mu.Unlock()
	counts := stats.counts
	var problems []error
	var phaseAttempts uint64
	for _, phase := range []soakHTTPPhase{soakHTTPPrefill, soakHTTPAdmission, soakHTTPActive, soakHTTPCacheFull, soakHTTPQueueFull, soakHTTPDisconnected} {
		phaseAttempts += counts["phase_"+string(phase)+"_attempts"]
	}
	if phaseAttempts != counts["all_http_attempts"] || phaseAttempts-counts["phase_prefill_attempts"] != counts["http_attempts"] {
		problems = append(problems, errors.New("HTTP phase/all/measured attempt denominators disagree"))
	}
	for _, prefix := range []string{"all_http_", "http_"} {
		attempted, responses, clientErrors := counts[prefix+"attempts"], counts[prefix+"requests"], counts[prefix+"client_error"]
		valid, invalid := counts[prefix+"valid_responses"], counts[prefix+"invalid_responses"]
		var statuses, reasons uint64
		for key, n := range counts {
			if strings.HasPrefix(key, prefix) {
				tail := strings.TrimPrefix(key, prefix)
				if code, err := strconv.Atoi(tail); err == nil && code >= 100 && code <= 599 {
					statuses += n
				}
			}
		}
		for _, reason := range []string{"read_error", "body_error", "header_error", "decode_error", "schema_error", "status_error", "semantic_error"} {
			reasons += counts[prefix+reason]
		}
		if attempted != responses+clientErrors {
			problems = append(problems, fmt.Errorf("%sattempts=%d != responses=%d + client_errors=%d", prefix, attempted, responses, clientErrors))
		}
		if statuses != responses {
			problems = append(problems, fmt.Errorf("%sstatus sum=%d != responses=%d", prefix, statuses, responses))
		}
		if responses != valid+invalid || invalid != reasons {
			problems = append(problems, fmt.Errorf("%sresponse classification mismatch responses=%d valid=%d invalid=%d reasons=%d", prefix, responses, valid, invalid, reasons))
		}
		if valid != counts[prefix+"valid_202"]+counts[prefix+"valid_429"]+counts[prefix+"valid_503"] {
			problems = append(problems, fmt.Errorf("%svalid-status count mismatch", prefix))
		}
		if clientErrors > 0 || invalid > 0 {
			problems = append(problems, fmt.Errorf("%sfail-closed: client_errors=%d invalid_responses=%d", prefix, clientErrors, invalid))
		}
	}
	for _, key := range []string{"fixture_decode_error", "fixture_hash_error", "unexpected_fixture_code", "observer_error"} {
		if counts[key] > 0 {
			problems = append(problems, fmt.Errorf("%s=%d", key, counts[key]))
		}
	}
	return errors.Join(problems...)
}

// Fault injections use a private RoundTripper and separate statistics. They do
// not run in the timed workload or contribute to a performance report.
type soakRoundTripFunc func(*http.Request) (*http.Response, error)

func (f soakRoundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

type soakErrorReader struct{}

func (soakErrorReader) Read([]byte) (int, error) { return 0, io.ErrUnexpectedEOF }

type soakCloseErrorBody struct{ io.Reader }

func (soakCloseErrorBody) Close() error { return errors.New("injected close failure") }
func soakTestAPI(stats *soakStats, status int, payload, contentType string, readError, closeError, clientError bool) *soakAPI {
	transport := soakRoundTripFunc(func(*http.Request) (*http.Response, error) {
		if clientError {
			return nil, errors.New("injected client failure")
		}
		var reader io.Reader = strings.NewReader(payload)
		if readError {
			reader = io.MultiReader(reader, soakErrorReader{})
		}
		var body io.ReadCloser = io.NopCloser(reader)
		if closeError {
			body = soakCloseErrorBody{reader}
		}
		return &http.Response{StatusCode: status, Header: http.Header{"Content-Type": []string{contentType}}, Body: body}, nil
	})
	return &soakAPI{server: &httptest.Server{URL: "http://127.0.0.1:1"}, client: &http.Client{Transport: transport}, stats: stats}
}
func soakSubmissionJSON(t *testing.T, fixture soakRequest, queued int, duplicate bool, stage string) string {
	t.Helper()
	raw, err := json.Marshal(Submission{Hash: fixture.hash, Sender: fixture.sender, QueuedPeers: queued, Duplicate: duplicate, Stage: stage})
	if err != nil {
		t.Fatal(err)
	}
	return string(raw)
}
func TestSoakHTTPFaultsFailClosed(t *testing.T) {
	fixture := soakBodies(t, 1)[0]
	valid := soakSubmissionJSON(t, fixture, 1, false, "queued")
	errorJSON := func(message string) string {
		raw, _ := json.Marshal(map[string]string{"error": message})
		return string(raw)
	}
	cases := []struct {
		name                               string
		status                             int
		body, reason                       string
		phase                              soakHTTPPhase
		readError, closeError, clientError bool
		contentType                        string
	}{
		{name: "500", status: 500, body: errorJSON("injected failure"), reason: "status_error"},
		{name: "unexpected_400", status: 400, body: errorJSON("invalid JSON request"), reason: "status_error"},
		{name: "unexpected_503_while_peer_ready", status: 503, body: errorJSON(ErrNoPeers.Error()), reason: "status_error"},
		{name: "unexpected_200", status: 200, body: valid, reason: "status_error"},
		{name: "redirect", status: 302, body: errorJSON("redirect"), reason: "status_error"},
		{name: "truncated_body_after_valid_json", status: 202, body: valid, readError: true, reason: "read_error"},
		{name: "body_close_error", status: 202, body: valid, closeError: true, reason: "read_error"},
		{name: "invalid_json", status: 202, body: `{"hash":`, reason: "decode_error"},
		{name: "wrong_schema", status: 202, body: `{}`, reason: "schema_error"},
		{name: "unknown_field", status: 202, body: strings.TrimSuffix(valid, "}") + `,"surprise":true}`, reason: "schema_error"},
		{name: "duplicate_field", status: 202, body: strings.TrimSuffix(valid, "}") + `,"stage":"queued"}`, reason: "schema_error"},
		{name: "missing_field", status: 202, body: strings.Replace(valid, `"duplicate":false,`, "", 1), reason: "schema_error"},
		{name: "null_field", status: 202, body: strings.Replace(valid, `"queued_peers":1`, `"queued_peers":null`, 1), reason: "schema_error"},
		{name: "wrong_field_type", status: 202, body: strings.Replace(valid, `"queued_peers":1`, `"queued_peers":"1"`, 1), reason: "schema_error"},
		{name: "trailing_json", status: 202, body: valid + ` {}`, reason: "decode_error"},
		{name: "wrong_hash", status: 202, body: strings.Replace(valid, fixture.hash.Hex(), common.Hash{1}.Hex(), 1), reason: "semantic_error"},
		{name: "wrong_sender", status: 202, body: strings.Replace(valid, strings.ToLower(fixture.sender.Hex()), common.Address{1}.Hex(), 1), reason: "semantic_error"},
		{name: "negative_queue_count", status: 202, body: soakSubmissionJSON(t, fixture, -1, false, "queued"), reason: "semantic_error"},
		{name: "queue_count_exceeds_peer_count", status: 202, body: soakSubmissionJSON(t, fixture, 4, false, "queued"), reason: "semantic_error"},
		{name: "wrong_stage", status: 202, body: soakSubmissionJSON(t, fixture, 1, false, "cached"), reason: "semantic_error"},
		{name: "cached_not_duplicate", status: 202, body: soakSubmissionJSON(t, fixture, 0, false, "cached"), reason: "semantic_error"},
		{name: "reject_invalid_json", status: 429, body: `oops`, reason: "decode_error"},
		{name: "reject_wrong_schema", status: 429, body: `{"error":123}`, reason: "schema_error"},
		{name: "reject_unknown_error", status: 429, body: errorJSON("unknown failure"), reason: "semantic_error"},
		{name: "cache_error_in_active_hotset", status: 429, body: errorJSON(ErrCacheFull.Error()), reason: "semantic_error"},
		{name: "wrong_full_cache_reason", status: 429, body: errorJSON(ErrQueuesFull.Error()), phase: soakHTTPCacheFull, reason: "semantic_error"},
		{name: "wrong_disconnect_reason", status: 503, body: errorJSON(ErrQueuesFull.Error()), phase: soakHTTPDisconnected, reason: "semantic_error"},
		{name: "success_in_disconnected_probe", status: 202, body: valid, phase: soakHTTPDisconnected, reason: "status_error"},
		{name: "wrong_media_type", status: 202, body: valid, contentType: "text/plain", reason: "header_error"},
		{name: "oversized_body", status: 202, body: valid + strings.Repeat(" ", 8193), reason: "body_error"},
		{name: "client_failure", clientError: true, reason: "client_error"},
		{name: "prefill_500", status: 500, body: errorJSON("prefill failure"), phase: soakHTTPPrefill, reason: "status_error"},
		{name: "expiry_500", status: 500, body: errorJSON("expiry failure"), phase: soakHTTPAdmission, reason: "status_error"},
		{name: "expiry_invalid_json", status: 202, body: `{`, phase: soakHTTPAdmission, reason: "decode_error"},
		{name: "expiry_duplicate_not_fresh", status: 202, body: soakSubmissionJSON(t, fixture, 1, true, "queued"), phase: soakHTTPAdmission, reason: "semantic_error"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			phase := tc.phase
			if phase == "" {
				phase = soakHTTPActive
			}
			media := tc.contentType
			if media == "" {
				media = "application/json"
			}
			stats := newSoakStats()
			api := soakTestAPI(stats, tc.status, tc.body, media, tc.readError, tc.closeError, tc.clientError)
			_, _, err := api.submit(context.Background(), fixture, phase)
			if err == nil {
				t.Fatal("injected invalid response unexpectedly accepted")
			}
			if got := stats.get("all_http_" + tc.reason); got != 1 {
				t.Fatalf("reason %s count=%d, err=%v", tc.reason, got, err)
			}
			if err := soakValidateScenario(stats); err == nil {
				t.Fatal("injected failure made final scenario validation green")
			}
			if stats.get("queue_admissions") != 0 {
				t.Fatal("invalid response affected queue-admission denominator")
			}
			if stats.get("all_http_attempts") != 1 {
				t.Fatal("missing attempted request")
			}
			responses, clients := uint64(1), uint64(0)
			if tc.clientError {
				responses, clients = 0, 1
			}
			if stats.get("all_http_requests") != responses || stats.get("all_http_client_error") != clients {
				t.Fatal("attempt/response/client-error accounting mismatch")
			}
			if !phase.measured() && stats.get("http_attempts") != 0 {
				t.Fatal("prefill leaked into measured HTTP denominator")
			}
		})
	}
}
func TestSoakHTTPValidPhaseResponses(t *testing.T) {
	fixture := soakBodies(t, 1)[0]
	fresh := soakSubmissionJSON(t, fixture, 1, false, "queued")
	cached := soakSubmissionJSON(t, fixture, 0, true, "cached")
	for _, tc := range []struct {
		name   string
		phase  soakHTTPPhase
		status int
		body   string
	}{
		{"prefill", soakHTTPPrefill, 202, fresh}, {"expiry", soakHTTPAdmission, 202, fresh}, {"active_queued", soakHTTPActive, 202, fresh}, {"active_cached", soakHTTPActive, 202, cached},
		{"queue_pressure", soakHTTPActive, 429, `{"error":"` + ErrQueuesFull.Error() + `"}`},
		{"concurrency_pressure", soakHTTPActive, 429, `{"error":"too many concurrent requests"}`},
		{"admission_expiry", soakHTTPActive, 429, `{"error":"` + ErrAdmissionExpired.Error() + `"}`},
		{"cache_probe", soakHTTPCacheFull, 429, `{"error":"` + ErrCacheFull.Error() + `"}`},
		{"queue_probe", soakHTTPQueueFull, 429, `{"error":"` + ErrQueuesFull.Error() + `"}`},
		{"disconnect_probe", soakHTTPDisconnected, 503, `{"error":"` + ErrNoPeers.Error() + `"}`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			stats := newSoakStats()
			api := soakTestAPI(stats, tc.status, tc.body, "application/json", false, false, false)
			if _, _, err := api.submit(context.Background(), fixture, tc.phase); err != nil {
				t.Fatal(err)
			}
			if err := soakValidateScenario(stats); err != nil {
				t.Fatal(err)
			}
		})
	}
}
func TestSoakHTTPAccountingFailsClosed(t *testing.T) {
	fixture := soakBodies(t, 1)[0]
	valid := soakSubmissionJSON(t, fixture, 1, false, "queued")
	for _, key := range []string{"all_http_attempts", "http_attempts", "phase_active_load_attempts", "phase_prefill_attempts", "all_http_requests", "http_requests", "all_http_500", "http_500", "all_http_valid_responses", "http_valid_responses", "all_http_invalid_responses", "http_invalid_responses", "all_http_read_error", "http_read_error", "all_http_valid_202", "http_valid_202", "fixture_decode_error", "fixture_hash_error", "observer_error"} {
		t.Run(key, func(t *testing.T) {
			stats := newSoakStats()
			api := soakTestAPI(stats, 202, valid, "application/json", false, false, false)
			if _, _, err := api.submit(context.Background(), fixture, soakHTTPActive); err != nil {
				t.Fatal(err)
			}
			stats.count(key, 1)
			if err := soakValidateScenario(stats); err == nil {
				t.Fatal("corrupt denominator/error count accepted")
			}
		})
	}
}
