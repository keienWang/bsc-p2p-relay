// SPDX-License-Identifier: GPL-3.0-or-later
package relay

import (
	"encoding/hex"
	"fmt"
	"io"
	"sync/atomic"
	"time"
)

// Zero is deliberately other: an unset/unsupported classification is not success.
type submitOutcome uint8

const (
	submitOther submitOutcome = iota
	submitAcceptedNew
	submitAcceptedCached
	submitRejectedValidation
	submitRejectedNoPeers
	submitRejectedCacheFull
	submitRejectedQueueUnavailable
	submitRejectedAdmissionExpired
	submitOutcomeCount
)

type statsIdentity struct {
	id      string
	created time.Time
}

// The production caller supplies crypto/rand.Reader. This identity is public
// correlation metadata, not an authentication credential or a P2P identity.
func newStatsIdentity(entropy io.Reader) (statsIdentity, error) {
	var id [16]byte
	if _, err := io.ReadFull(entropy, id[:]); err != nil {
		return statsIdentity{}, fmt.Errorf("generate statistics instance identity: %w", err)
	}
	return statsIdentity{id: hex.EncodeToString(id[:]), created: time.Now().UTC()}, nil
}

type runtimeStats struct {
	identity            statsIdentity // Immutable after Node construction.
	outcomes            [submitOutcomeCount]atomic.Uint64
	concurrencyRejected atomic.Uint64
}

func (s *runtimeStats) record(outcome submitOutcome) {
	if outcome >= submitOutcomeCount {
		outcome = submitOther
	}
	s.outcomes[outcome].Add(1)
}

// Stats is the frozen /v1/stats schema. New schemas must use a separate endpoint.
// Individual counters are race-safe; this is not one global atomic snapshot.
type Stats struct {
	SchemaVersion int         `json:"schema_version"`
	InstanceID    string      `json:"instance_id"`
	CreatedAt     time.Time   `json:"created_at"`
	ObservedAt    time.Time   `json:"observed_at"`
	Submit        SubmitStats `json:"submit"`
	HTTP          HTTPStats   `json:"http"`
}

type SubmitStats struct {
	AcceptedNewCacheLifetimeTotal uint64 `json:"accepted_new_cache_lifetime_total"`
	AcceptedCachedRetryTotal      uint64 `json:"accepted_cached_retry_total"`
	RejectedValidationTotal       uint64 `json:"rejected_validation_total"`
	RejectedNoPeersTotal          uint64 `json:"rejected_no_peers_total"`
	RejectedCacheFullTotal        uint64 `json:"rejected_cache_full_total"`
	RejectedQueueUnavailableTotal uint64 `json:"rejected_queue_unavailable_total"`
	RejectedAdmissionExpiredTotal uint64 `json:"rejected_admission_expired_total"`
	OtherTotal                    uint64 `json:"other_total"`
}

type HTTPStats struct {
	ConcurrencyRejectedTotal uint64 `json:"concurrency_rejected_total"`
}

// Stats reads only fixed atomics and immutable metadata. In particular it does
// not take submit/cache/peer locks, prune the cache, or use transaction slots.
func (n *Node) Stats() Stats {
	observed := time.Now().UTC()
	s := &n.stats
	return Stats{
		SchemaVersion: 1,
		InstanceID:    s.identity.id,
		CreatedAt:     s.identity.created,
		ObservedAt:    observed,
		Submit: SubmitStats{
			AcceptedNewCacheLifetimeTotal: s.outcomes[submitAcceptedNew].Load(),
			AcceptedCachedRetryTotal:      s.outcomes[submitAcceptedCached].Load(),
			RejectedValidationTotal:       s.outcomes[submitRejectedValidation].Load(),
			RejectedNoPeersTotal:          s.outcomes[submitRejectedNoPeers].Load(),
			RejectedCacheFullTotal:        s.outcomes[submitRejectedCacheFull].Load(),
			RejectedQueueUnavailableTotal: s.outcomes[submitRejectedQueueUnavailable].Load(),
			RejectedAdmissionExpiredTotal: s.outcomes[submitRejectedAdmissionExpired].Load(),
			OtherTotal:                    s.outcomes[submitOther].Load(),
		},
		HTTP: HTTPStats{ConcurrencyRejectedTotal: s.concurrencyRejected.Load()},
	}
}
