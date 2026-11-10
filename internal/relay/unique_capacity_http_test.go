// SPDX-License-Identifier: GPL-3.0-or-later
package relay

import (
	"encoding/json"
	"errors"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/ethereum/go-ethereum/common"
)

func TestUniqueHTTPContractsFailClosed(t *testing.T) {
	f := newUniqueGenerator(t).next(t, 0)
	accepted := soakSubmissionJSON(t, f.soakRequest, 1, false, "queued")
	rejection := func(message string) string {
		payload, err := json.Marshal(map[string]string{"error": message})
		if err != nil {
			t.Fatal(err)
		}
		return string(payload)
	}
	cases := []struct {
		name     string
		status   int
		body     string
		contract uniqueContract
		valid    bool
	}{
		{"fresh_accepted", 202, accepted, uniqueAccept(false, 1, 1), true},
		{"retry_cached", 202, soakSubmissionJSON(t, f.soakRequest, 0, true, "cached"), uniqueAccept(true, 0, 0), true},
		{"cache_full", 429, rejection(ErrCacheFull.Error()), uniqueReject(429, ErrCacheFull), true},
		{"queues_full", 429, rejection(ErrQueuesFull.Error()), uniqueReject(429, ErrQueuesFull), true},
		{"no_peers", 503, rejection(ErrNoPeers.Error()), uniqueReject(503, ErrNoPeers), true},
		{"unexpected_400", 400, rejection("bad request"), uniqueAccept(false, 1, 1), false},
		{"unexpected_500", 500, rejection("failure"), uniqueAccept(false, 1, 1), false},
		{"unexpected_503", 503, rejection(ErrNoPeers.Error()), uniqueAccept(false, 1, 1), false},
		{"unexpected_success", 202, accepted, uniqueReject(429, ErrCacheFull), false},
		{"fresh_duplicate_true", 202, soakSubmissionJSON(t, f.soakRequest, 1, true, "queued"), uniqueAccept(false, 1, 1), false},
		{"fresh_zero_queued", 202, soakSubmissionJSON(t, f.soakRequest, 0, true, "cached"), uniqueAccept(false, 1, 1), false},
		{"wrong_fanout", 202, soakSubmissionJSON(t, f.soakRequest, 2, false, "queued"), uniqueAccept(false, 1, 1), false},
		{"wrong_429_reason", 429, rejection(ErrQueuesFull.Error()), uniqueReject(429, ErrCacheFull), false},
		{"unexpected_concurrency_429", 429, rejection("too many concurrent requests"), uniqueReject(429, ErrCacheFull), false},
		{"unexpected_expired_429", 429, rejection(ErrAdmissionExpired.Error()), uniqueReject(429, ErrCacheFull), false},
		{"missing_schema", 202, `{}`, uniqueAccept(false, 1, 1), false},
		{"null_queue", 202, strings.Replace(accepted, `"queued_peers":1`, `"queued_peers":null`, 1), uniqueAccept(false, 1, 1), false},
		{"duplicate_field", 202, strings.TrimSuffix(accepted, "}") + `,"stage":"queued"}`, uniqueAccept(false, 1, 1), false},
		{"unknown_field", 202, strings.TrimSuffix(accepted, "}") + `,"extra":true}`, uniqueAccept(false, 1, 1), false},
		{"trailing_json", 202, accepted + ` {}`, uniqueAccept(false, 1, 1), false},
		{"wrong_hash", 202, strings.Replace(accepted, f.hash.Hex(), common.Hash{1}.Hex(), 1), uniqueAccept(false, 1, 1), false},
		{"unknown_rejection_contract", 429, rejection("unknown"), uniqueReject(429, errors.New("unknown")), false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, _, err := uniqueValidateHTTP(tc.status, "application/json", []byte(tc.body), f.soakRequest, tc.contract)
			if (err == nil) != tc.valid {
				t.Fatalf("valid=%v error=%v", tc.valid, err)
			}
		})
	}
}

func TestUniqueHTTPTransportFaultsRetainDenominators(t *testing.T) {
	for _, fault := range []string{"client", "read", "close", "oversize", "content_type"} {
		t.Run(fault, func(t *testing.T) {
			f := newUniqueGenerator(t).next(t, 0)
			body := soakSubmissionJSON(t, f.soakRequest, 1, false, "queued")
			content := "application/json"
			if fault == "oversize" {
				body += strings.Repeat(" ", 8193)
			}
			if fault == "content_type" {
				content = "text/plain"
			}
			stats := newSoakStats()
			h := &uniqueHarness{start: time.Now(), stats: stats, counts: newUniqueCounts(), ledger: make(map[common.Hash]*uniqueHashLedger), phases: make(map[string]*uniqueCounts)}
			h.api = soakTestAPI(stats, 202, body, content, fault == "read", fault == "close", fault == "client")
			if _, _, err := h.submit(f, "new_fault_probe", false, uniqueAccept(false, 1, 1)); err == nil {
				t.Fatal("fault passed")
			}
			if h.counts.Attempts != 1 || h.counts.OfferedUnique != 1 || h.counts.Retries != 0 || h.counts.Accepted != 0 || h.counts.QueuedPeers != 0 {
				t.Fatalf("fault counters: %+v", h.counts)
			}
			if fault == "client" {
				if h.counts.ClientErrors != 1 || h.counts.Responses != 0 {
					t.Fatal("client error denominator")
				}
			} else if h.counts.Invalid != 1 || h.counts.Responses != 1 {
				t.Fatal("invalid response denominator")
			}
			if len(h.failures) != 1 || uniqueAuditCounts(h.counts) == nil {
				t.Fatal("fault did not fail closed with retained diagnostic")
			}
		})
	}
}

func TestUniqueCountAuditRejectsBrokenPartitions(t *testing.T) {
	c := newUniqueCounts()
	c.Attempts, c.OfferedUnique, c.Responses, c.Accepted = 1, 1, 1, 1
	c.Statuses["202"] = 1
	if err := uniqueAuditCounts(c); err != nil {
		t.Fatal(err)
	}
	c.Retries = 1
	if uniqueAuditCounts(c) == nil {
		t.Fatal("accepted attempts != offered unique + retries")
	}
	c.Retries = 0
	c.RejectReasons[ErrCacheFull.Error()] = 1
	if uniqueAuditCounts(c) == nil {
		t.Fatal("accepted rejection reasons without rejected response")
	}
}

func TestUniqueTransportAuditDoesNotInventSkips(t *testing.T) {
	for _, kind := range []string{"more_attempts_than_admissions", "incomplete_write", "observer_error"} {
		t.Run(kind, func(t *testing.T) {
			s := newSoakStats()
			s.count("write_attempts", 1)
			if kind != "more_attempts_than_admissions" {
				s.count("queue_admissions", 1)
			}
			if kind != "incomplete_write" {
				s.count("write_completed", 1)
			}
			if kind == "observer_error" {
				s.count("observer_error", 1)
			}
			if uniqueTransportConservation(s) == nil || s.get("writer_skipped_expired_by_conservation") != 0 {
				t.Fatal("invalid transport ledger accepted or expiry skips invented")
			}
		})
	}
}

// Intentionally exits nonzero. Run alone with --failure-probe to verify that a
// Fatal HTTP gate failure still writes failed scenario/global JSON and counters.
// It is not run as a nested subprocess or mixed into a performance measurement.
func TestUniqueCapacityFailureReport(t *testing.T) {
	if os.Getenv("RELAY_UNIQUE_FAILURE_PROBE") != "1" {
		t.Skip("isolated negative probe: scripts/run_unique_capacity.sh --failure-probe")
	}
	report := uniqueReport(t, "intentional HTTP-500 failure; expected nonzero exit")
	t.Run("injected_500", func(t *testing.T) {
		h := newUniqueHarness(t, 4, MaxRawTransaction, time.Minute, report, t.Name())
		h.api.client = soakTestAPI(newSoakStats(), 500, `{"error":"injected failure"}`, "application/json", false, false, false).client
		h.mustSubmit(t, h.gen.next(t, 0), "new_injected_500", false, uniqueAccept(false, 1, 1))
	})
}
