// SPDX-License-Identifier: GPL-3.0-or-later
package relay

// An independent, test-only v1 consumer. It deliberately does not use Stats,
// SubmitStats, submitOutcome, Node.stats, or Node.Stats as its oracle.
import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/http/httptest"
	"regexp"
	"strconv"
	"strings"
	"testing"
	"time"
)

const (
	ledgerNew = iota
	ledgerCached
	ledgerValidation
	ledgerNoPeers
	ledgerCacheFull
	ledgerQueue
	ledgerExpired
	ledgerOther
	ledgerConcurrency
)

var ledgerFields = [...]string{
	"accepted_new_cache_lifetime_total", "accepted_cached_retry_total",
	"rejected_validation_total", "rejected_no_peers_total", "rejected_cache_full_total",
	"rejected_queue_unavailable_total", "rejected_admission_expired_total", "other_total",
	"concurrency_rejected_total",
}

type ledgerVector [9]uint64

type ledgerSnapshot struct {
	instance, created string
	counters          ledgerVector
}

type ledgerExpectation struct {
	counters      ledgerVector
	normalReturns uint64 // Independently counted completed Submit calls, not sum(counters).
	unknown       uint64 // Client outcomes without execution evidence are NOT rejections.
}

func ledgerDecode(status int, headers http.Header, body []byte) (ledgerSnapshot, error) {
	fail := func(err error) (ledgerSnapshot, error) { return ledgerSnapshot{}, err }
	if status != 200 || headers.Get("Content-Type") != "application/json" || headers.Get("Cache-Control") != "no-store" || headers.Get("X-Content-Type-Options") != "nosniff" {
		return fail(errors.New("unavailable stats status/headers"))
	}
	root, err := soakStrictObject(body, "schema_version", "instance_id", "created_at", "observed_at", "submit", "http")
	if err != nil {
		return fail(err)
	}
	if string(root["schema_version"]) != "1" {
		return fail(errors.New("unknown or invalid schema version"))
	}
	var sample ledgerSnapshot
	if err = json.Unmarshal(root["instance_id"], &sample.instance); err != nil || !regexp.MustCompile(`^[0-9a-f]{32}$`).MatchString(sample.instance) {
		return fail(errors.New("invalid instance identity"))
	}
	for _, key := range []string{"created_at", "observed_at"} {
		var value string
		if err = json.Unmarshal(root[key], &value); err != nil || !strings.HasSuffix(value, "Z") {
			return fail(fmt.Errorf("invalid UTC %s", key))
		}
		stamp, err := time.Parse(time.RFC3339Nano, value)
		if err != nil || stamp.IsZero() {
			return fail(fmt.Errorf("invalid %s", key))
		}
		if key == "created_at" {
			sample.created = value
		}
	}
	// Descriptive wall-clock timestamps are not ordered or used as durations.
	submit, err := soakStrictObject(root["submit"], ledgerFields[:8]...)
	if err != nil {
		return fail(err)
	}
	httpFields, err := soakStrictObject(root["http"], ledgerFields[8])
	if err != nil {
		return fail(err)
	}
	for i, field := range ledgerFields {
		raw := submit[field]
		if i == ledgerConcurrency {
			raw = httpFields[field]
		}
		// Parse the raw token directly: no float64 round trip, null, exponent,
		// fraction, signed value, string coercion or overflowing uint64.
		value, err := strconv.ParseUint(string(raw), 10, 64)
		if err != nil {
			return fail(fmt.Errorf("invalid uint64 %s: %w", field, err))
		}
		sample.counters[i] = value
	}
	return sample, nil
}

func ledgerReadResponse(response *http.Response) (ledgerSnapshot, error) {
	body, readErr := io.ReadAll(io.LimitReader(response.Body, 8193))
	closeErr := response.Body.Close()
	if err := errors.Join(readErr, closeErr); err != nil {
		return ledgerSnapshot{}, err
	}
	if len(body) > 8192 {
		return ledgerSnapshot{}, errors.New("oversized stats response")
	}
	if response.ContentLength >= 0 && int64(len(body)) != response.ContentLength {
		return ledgerSnapshot{}, errors.New("incomplete stats Content-Length")
	}
	return ledgerDecode(response.StatusCode, response.Header, body)
}

func ledgerScrape(client *http.Client, url string) (ledgerSnapshot, error) {
	response, err := client.Get(url + "/v1/stats")
	if err != nil {
		return ledgerSnapshot{}, err
	}
	return ledgerReadResponse(response)
}

func ledgerHTTPSnapshot(t *testing.T, h *uniqueHarness) ledgerSnapshot {
	t.Helper()
	sample, err := ledgerScrape(h.api.client, h.api.server.URL)
	if err != nil {
		t.Fatal(err)
	}
	return sample
}

func ledgerHandlerSnapshot(t *testing.T, handler http.Handler) ledgerSnapshot {
	t.Helper()
	response := statsRequest(handler, "GET", "/v1/stats", "")
	sample, err := ledgerDecode(response.Code, response.Header(), response.Body.Bytes())
	if err != nil {
		t.Fatal(err)
	}
	return sample
}

// Call only after all submission callers have joined. Snapshot equality during
// concurrent admission is deliberately NOT a supported accounting assertion.
func ledgerCompare(before, after ledgerSnapshot, expected ledgerExpectation) error {
	if expected.unknown != 0 {
		return errors.New("incomparable: client outcome does not establish execution")
	}
	if before.instance == "" || before.created == "" || before.instance != after.instance || before.created != after.created {
		return errors.New("incomparable: missing or changed instance/creation metadata")
	}
	var returned uint64
	for i, field := range ledgerFields {
		if after.counters[i] < before.counters[i] {
			return fmt.Errorf("incomparable: counter decreased: %s", field)
		}
		delta := after.counters[i] - before.counters[i]
		if delta != expected.counters[i] {
			return fmt.Errorf("%s delta=%d want=%d", field, delta, expected.counters[i])
		}
		if i != ledgerConcurrency {
			if math.MaxUint64-returned < delta {
				return errors.New("incomparable: Submit sum overflow")
			}
			returned += delta
		}
	}
	if returned != expected.normalReturns {
		return fmt.Errorf("Submit denominator=%d want known normal returns=%d", returned, expected.normalReturns)
	}
	return nil
}

// This adapter only accepts the existing harness's completely validated HTTP
// ledger. Error strings here are declared test response contracts, never a new
// production classification mechanism. Unknown partitions fail closed.
func ledgerFromUnique(c *uniqueCounts) (ledgerExpectation, error) {
	// The legacy harness also uses uint64 arithmetic; reject a forged wrapped
	// partition before using its successful audit as evidence.
	partitions := [][]uint64{
		{c.OfferedUnique, c.Retries}, {c.Responses, c.ClientErrors},
		{c.Accepted, c.Rejected429, c.Rejected503, c.Invalid},
	}
	for _, counts := range []map[string]uint64{c.Statuses, c.RejectReasons} {
		var partition []uint64
		for _, count := range counts {
			partition = append(partition, count)
		}
		partitions = append(partitions, partition)
	}
	for _, partition := range partitions {
		var sum uint64
		for _, count := range partition {
			if math.MaxUint64-sum < count {
				return ledgerExpectation{}, errors.New("overflowing unique ledger partition")
			}
			sum += count
		}
	}
	if err := uniqueAuditCounts(c); err != nil {
		return ledgerExpectation{}, err
	}
	expected := ledgerExpectation{normalReturns: c.Responses}
	expected.counters[ledgerNew] = c.Accepted - c.Duplicate
	expected.counters[ledgerCached] = c.Duplicate
	var classified, status429, status503 uint64
	for reason, count := range c.RejectReasons {
		var index int
		switch reason {
		case ErrCacheFull.Error():
			index = ledgerCacheFull
		case ErrQueuesFull.Error():
			index = ledgerQueue
		case ErrAdmissionExpired.Error():
			index = ledgerExpired
		case ErrNoPeers.Error():
			index = ledgerNoPeers
		default:
			return ledgerExpectation{}, fmt.Errorf("unknown unique HTTP partition %q", reason)
		}
		expected.counters[index] += count
		classified += count
		if index == ledgerNoPeers {
			status503 += count
		} else {
			status429 += count
		}
	}
	if classified != c.Rejected429+c.Rejected503 || status429 != c.Rejected429 || status503 != c.Rejected503 || c.Statuses["202"] != c.Accepted || c.Statuses["429"] != c.Rejected429 || c.Statuses["503"] != c.Rejected503 {
		return ledgerExpectation{}, errors.New("unclassified or contradictory unique HTTP responses")
	}
	for status, count := range c.Statuses {
		if status != "202" && status != "429" && status != "503" && count != 0 {
			return ledgerExpectation{}, errors.New("unknown unique HTTP status")
		}
	}
	return expected, nil
}

func ledgerCheckUnique(t *testing.T, h *uniqueHarness, before ledgerSnapshot, plan ledgerExpectation, offered, retries uint64) {
	t.Helper()
	h.audit(t)
	expected, err := ledgerFromUnique(h.counts)
	if err != nil {
		t.Fatal(err)
	}
	if expected != plan || h.counts.OfferedUnique != offered || h.counts.Retries != retries {
		t.Fatalf("response ledger differs from declared scenario: got=%+v plan=%+v offered=%d/%d retries=%d/%d", expected, plan, h.counts.OfferedUnique, offered, h.counts.Retries, retries)
	}
	if err := ledgerCompare(before, ledgerHTTPSnapshot(t, h), expected); err != nil {
		t.Fatal(err)
	}
}

func ledgerSyntheticBody(values ledgerVector) []byte {
	// Literal independent metadata, not a production Stats marshaling helper.
	var out bytes.Buffer
	out.WriteString(`{"schema_version":1,"instance_id":"0123456789abcdef0123456789abcdef","created_at":"2026-01-02T03:04:05Z","observed_at":"2026-01-02T03:04:06.123456789Z","submit":{`)
	for i, field := range ledgerFields[:8] {
		if i > 0 {
			out.WriteByte(',')
		}
		fmt.Fprintf(&out, "%q:%d", field, values[i])
	}
	fmt.Fprintf(&out, `},"http":{"concurrency_rejected_total":%d}}`, values[8])
	return out.Bytes()
}

func ledgerHeaders() http.Header {
	return http.Header{"Content-Type": {"application/json"}, "Cache-Control": {"no-store"}, "X-Content-Type-Options": {"nosniff"}}
}

func TestStatsLedgerMutations(t *testing.T) {
	// Distinct values make ALL 36 pair swaps observable, including other/HTTP.
	// Nonzero other is parser/auditor fault data, not a reachable normal fixture.
	values := ledgerVector{11, 22, 33, 44, 55, 66, 77, 88, 99}
	after, err := ledgerDecode(200, ledgerHeaders(), ledgerSyntheticBody(values))
	if err != nil {
		t.Fatal(err)
	}
	before := after
	before.counters = ledgerVector{}
	plan := ledgerExpectation{counters: values, normalReturns: 396}
	if err := ledgerCompare(before, after, plan); err != nil {
		t.Fatal(err)
	}
	for i, field := range ledgerFields {
		for _, change := range []int{-1, 1} {
			t.Run(fmt.Sprintf("%s/change_%d", field, change), func(t *testing.T) {
				mutated := after
				mutated.counters[i] = uint64(int64(mutated.counters[i]) + int64(change))
				if ledgerCompare(before, mutated, plan) == nil {
					t.Fatal("missed under/overcount")
				}
			})
		}
		for j := i + 1; j < len(values); j++ {
			t.Run(field+"/swap_"+ledgerFields[j], func(t *testing.T) {
				mutated := values
				mutated[i], mutated[j] = mutated[j], mutated[i]
				sample, err := ledgerDecode(200, ledgerHeaders(), ledgerSyntheticBody(mutated))
				if err != nil || ledgerCompare(before, sample, plan) == nil {
					t.Fatalf("missed field swap or invalid mutation: %v", err)
				}
			})
		}
	}
	for _, wrong := range []uint64{11, 33, 395, 397, 495} {
		mutated := plan
		mutated.normalReturns = wrong
		if ledgerCompare(before, after, mutated) == nil {
			t.Fatalf("accepted wrong denominator %d", wrong)
		}
	}
	mutated := plan
	mutated.unknown = 1
	if ledgerCompare(before, after, mutated) == nil {
		t.Fatal("accepted unknown client execution")
	}
	changed := after
	changed.instance = strings.Repeat("a", 32)
	if ledgerCompare(before, changed, plan) == nil {
		t.Fatal("accepted changed instance")
	}
	changed = after
	changed.created = "2026-01-03T03:04:05Z"
	if ledgerCompare(before, changed, plan) == nil {
		t.Fatal("accepted changed creation metadata")
	}
	for i := range values {
		previous := before
		previous.counters[i] = after.counters[i] + 1
		if ledgerCompare(previous, after, plan) == nil {
			t.Fatal("accepted decreasing counter")
		}
	}
	// No wrapped sum may masquerade as one known return.
	overflow := after
	overflow.counters = ledgerVector{math.MaxUint64, 2}
	if ledgerCompare(before, overflow, ledgerExpectation{counters: overflow.counters, normalReturns: 1}) == nil {
		t.Fatal("accepted overflowing Submit sum")
	}
}

func TestStatsLedgerStrictResponses(t *testing.T) {
	base := string(ledgerSyntheticBody(ledgerVector{11, 22, 33, 44, 55, 66, 77, 88, 99}))
	reasons := make(map[string]string)
	bad := map[string]string{
		"truncated": base[:len(base)-1], "trailing": base + `{}`, "empty": `{}`,
		"unknown_schema": strings.Replace(base, `"schema_version":1`, `"schema_version":2`, 1),
		"version_string": strings.Replace(base, `"schema_version":1`, `"schema_version":"1"`, 1),
		"extra":          strings.Replace(base, `"schema_version":1`, `"extra":0,"schema_version":1`, 1),
		"duplicate":      strings.Replace(base, `"schema_version":1`, `"schema_version":1,"schema_version":1`, 1),
		"identity":       strings.Replace(base, "0123456789abcdef0123456789abcdef", "not-an-identity", 1),
		"non_utc":        strings.Replace(base, "03:04:05Z", "03:04:05+00:00", 1),
		"bad_date":       strings.Replace(base, "2026-01-02", "2026-99-99", 1),
	}
	for _, group := range []string{"submit", "http"} {
		bad[group+"/unknown"] = strings.Replace(base, `"`+group+`":{`, `"`+group+`":{"unexpected":1,`, 1)
	}
	var root map[string]json.RawMessage
	if err := json.Unmarshal([]byte(base), &root); err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{"schema_version", "instance_id", "created_at", "observed_at", "submit", "http"} {
		value := root[key]
		delete(root, key)
		body, err := json.Marshal(root)
		if err != nil {
			t.Fatal(err)
		}
		bad[key+"/missing_root"] = string(body)
		reasons[key+"/missing_root"] = "missing required response field"
		root[key] = json.RawMessage("null")
		body, err = json.Marshal(root)
		if err != nil {
			t.Fatal(err)
		}
		bad[key+"/null_root"] = string(body)
		reasons[key+"/null_root"] = fmt.Sprintf("null field %q", key)
		root[key] = value
	}
	for i, field := range ledgerFields {
		old := fmt.Sprintf("%q:%d", field, (i+1)*11)
		for _, value := range []string{"null", `"11"`, "true", "[]", "{}", "-1", "1.5", "1e1", "18446744073709551616"} {
			bad[field+"/"+value] = strings.Replace(base, old, fmt.Sprintf("%q:%s", field, value), 1)
			reasons[field+"/"+value] = "invalid uint64 " + field
			if value == "null" {
				reasons[field+"/"+value] = fmt.Sprintf("null field %q", field)
			}
		}
		bad[field+"/duplicate"] = strings.Replace(base, old, old+","+old, 1)
		reasons[field+"/duplicate"] = fmt.Sprintf("duplicate field %q", field)
		reasons[field+"/missing"] = "missing required response field"
		if i < 7 {
			bad[field+"/missing"] = strings.Replace(base, old+",", "", 1)
		} else if i == 7 {
			bad[field+"/missing"] = strings.Replace(base, ","+old, "", 1)
		} else {
			bad[field+"/missing"] = strings.Replace(base, old, "", 1)
		}
	}
	for name, body := range bad {
		t.Run(name, func(t *testing.T) {
			if name != "truncated" && name != "trailing" && !json.Valid([]byte(body)) {
				t.Fatal("semantic mutant has invalid JSON syntax")
			}
			sample, err := ledgerDecode(200, ledgerHeaders(), []byte(body))
			if err == nil || sample != (ledgerSnapshot{}) {
				t.Fatalf("bad response became a sample: %+v %v", sample, err)
			}
			if reason := reasons[name]; reason != "" && !strings.Contains(err.Error(), reason) {
				t.Fatalf("mutant failed for an unrelated reason: %v; want %q", err, reason)
			}
		})
	}
	for _, value := range []uint64{1<<53 + 1, math.MaxUint64} {
		var values ledgerVector
		for i := range values {
			values[i] = value
		}
		sample, err := ledgerDecode(200, ledgerHeaders(), ledgerSyntheticBody(values))
		if err != nil || sample.counters != values {
			t.Fatalf("uint64 precision: %v %v", sample.counters, err)
		}
	}
	for _, status := range []int{404, 429, 500, 503} {
		if _, err := ledgerDecode(status, ledgerHeaders(), []byte(base)); err == nil {
			t.Fatalf("accepted status %d", status)
		}
	}
	for _, header := range []string{"Content-Type", "Cache-Control", "X-Content-Type-Options"} {
		headers := ledgerHeaders()
		headers.Del(header)
		if _, err := ledgerDecode(200, headers, []byte(base)); err == nil {
			t.Fatalf("accepted missing header %s", header)
		}
	}
	for _, fault := range []string{"read", "close", "length", "oversize"} {
		t.Run(fault, func(t *testing.T) {
			body := base
			if fault == "oversize" {
				body += strings.Repeat(" ", 8193)
			}
			response := &http.Response{StatusCode: 200, Header: ledgerHeaders(), Body: io.NopCloser(strings.NewReader(body)), ContentLength: -1}
			switch fault {
			case "read":
				response.Body = &ledgerFaultBody{Reader: strings.NewReader(body), readErr: io.ErrUnexpectedEOF}
			case "close":
				response.Body = &ledgerFaultBody{Reader: strings.NewReader(body), closeErr: errors.New("fixture close error")}
			case "length":
				response.ContentLength = int64(len(body) + 1)
			}
			if sample, err := ledgerReadResponse(response); err == nil || sample != (ledgerSnapshot{}) {
				t.Fatalf("fault became a fresh sample: %+v %v", sample, err)
			}
		})
	}
	// Real HTTP truncated Content-Length, including a complete JSON prefix.
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		for key, values := range ledgerHeaders() {
			w.Header()[key] = values
		}
		w.Header().Set("Content-Length", strconv.Itoa(len(base)+1))
		_, _ = io.WriteString(w, base)
	}))
	defer server.Close()
	client := server.Client()
	client.Timeout = time.Second
	if sample, err := ledgerScrape(client, server.URL); err == nil || sample != (ledgerSnapshot{}) {
		t.Fatalf("truncated HTTP response accepted: %+v %v", sample, err)
	}
	server.Close()
	if sample, err := ledgerScrape(client, server.URL); err == nil || sample != (ledgerSnapshot{}) {
		t.Fatalf("transport failure returned old/zero sample without error: %+v %v", sample, err)
	}
}

type ledgerFaultBody struct {
	io.Reader
	readErr, closeErr error
}

func (b *ledgerFaultBody) Read(p []byte) (int, error) {
	n, err := b.Reader.Read(p)
	if err == io.EOF && b.readErr != nil {
		return n, b.readErr
	}
	return n, err
}
func (b *ledgerFaultBody) Close() error { return b.closeErr }

func TestStatsLedgerRejectsIncompleteUniqueLedger(t *testing.T) {
	valid := func() *uniqueCounts {
		c := newUniqueCounts()
		c.Attempts, c.OfferedUnique, c.Responses, c.Accepted = 2, 1, 2, 2
		c.Retries, c.Duplicate, c.QueuedPeers, c.Statuses["202"] = 1, 1, 2, 2
		return c
	}
	if _, err := ledgerFromUnique(valid()); err != nil {
		t.Fatal(err)
	}
	mutations := map[string]func(*uniqueCounts){
		"wrapped_partition":         func(c *uniqueCounts) { c.OfferedUnique = math.MaxUint64; c.Retries = 3 },
		"client_unknown":            func(c *uniqueCounts) { c.Attempts++; c.Retries++; c.ClientErrors++ },
		"invalid":                   func(c *uniqueCounts) { c.Invalid++; c.Accepted-- },
		"duplicate_underflow":       func(c *uniqueCounts) { c.Duplicate = 3 },
		"wrong_offered_denominator": func(c *uniqueCounts) { c.OfferedUnique++ },
		"wrong_status_partition":    func(c *uniqueCounts) { c.Statuses["202"]--; c.Statuses["500"]++ },
		"unknown_rejection": func(c *uniqueCounts) {
			c.Accepted--
			c.Rejected429++
			c.Statuses["202"]--
			c.Statuses["429"]++
			c.RejectReasons["unclassified"]++
		},
		"no_peers_as_429": func(c *uniqueCounts) {
			c.Accepted--
			c.Rejected429++
			c.Statuses["202"]--
			c.Statuses["429"]++
			c.RejectReasons[ErrNoPeers.Error()]++
		},
		"concurrency_not_submit": func(c *uniqueCounts) {
			c.Accepted--
			c.Rejected429++
			c.Statuses["202"]--
			c.Statuses["429"]++
			c.RejectReasons["too many concurrent requests"]++
		},
	}
	for name, mutate := range mutations {
		t.Run(name, func(t *testing.T) {
			c := valid()
			mutate(c)
			if _, err := ledgerFromUnique(c); err == nil {
				t.Fatal("incomplete/contradictory ledger accepted")
			}
		})
	}
}
