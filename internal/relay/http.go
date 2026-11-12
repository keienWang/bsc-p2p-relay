// SPDX-License-Identifier: GPL-3.0-or-later
package relay

import (
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"mime"
	"net"
	"net/http"
	"strings"
)

func LoopbackAddress(address string) error {
	host, _, err := net.SplitHostPort(address)
	if err != nil {
		return err
	}
	ip := net.ParseIP(host)
	if ip == nil || !ip.IsLoopback() {
		return errors.New("HTTP API must bind to a literal loopback IP in this milestone")
	}
	return nil
}
func jsonResponse(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
func apiError(w http.ResponseWriter, status int, message string) {
	jsonResponse(w, status, map[string]string{"error": message})
}
func (n *Node) Handler() http.Handler {
	slots := make(chan struct{}, 8)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		host := r.Host
		if h, _, err := net.SplitHostPort(host); err == nil {
			host = h
		}
		host = strings.Trim(host, "[]")
		ip := net.ParseIP(host)
		if host != "localhost" && (ip == nil || !ip.IsLoopback()) {
			apiError(w, 403, "invalid local Host header")
			return
		}
		if r.Header.Get("Origin") != "" {
			apiError(w, 403, "browser-origin requests are not allowed")
			return
		}
		switch r.URL.Path {
		case "/healthz", "/readyz":
			if r.Method != http.MethodGet {
				apiError(w, 405, "GET required")
				return
			}
			health := n.Health()
			status := 200
			if r.URL.Path == "/readyz" && health.ReadyPeers == 0 {
				status = 503
			}
			jsonResponse(w, status, health)
		case "/v1/stats":
			if r.Method != http.MethodGet {
				apiError(w, 405, "GET required")
				return
			}
			jsonResponse(w, 200, n.Stats())
		case "/v1/transactions":
			if r.Method != http.MethodPost {
				apiError(w, 405, "POST required")
				return
			}
			if media, _, err := mime.ParseMediaType(r.Header.Get("Content-Type")); err != nil || media != "application/json" {
				apiError(w, 415, "application/json required")
				return
			}
			select {
			case slots <- struct{}{}:
				defer func() { <-slots }()
			default:
				n.stats.concurrencyRejected.Add(1)
				apiError(w, 429, "too many concurrent requests")
				return
			}
			r.Body = http.MaxBytesReader(w, r.Body, 2*MaxRawTransaction+1024)
			defer r.Body.Close()
			var request struct {
				RawTx string `json:"raw_tx"`
			}
			dec := json.NewDecoder(r.Body)
			dec.DisallowUnknownFields()
			if err := dec.Decode(&request); err != nil {
				apiError(w, 400, "invalid JSON request")
				return
			}
			var extra any
			if err := dec.Decode(&extra); err != io.EOF {
				apiError(w, 400, "exactly one JSON object is required")
				return
			}
			if !strings.HasPrefix(request.RawTx, "0x") {
				apiError(w, 400, "raw_tx must be 0x-prefixed hex")
				return
			}
			raw, err := hex.DecodeString(request.RawTx[2:])
			if err != nil {
				apiError(w, 400, "invalid transaction hex")
				return
			}
			result, err := n.Submit(raw)
			if err != nil {
				status := 400
				if errors.Is(err, ErrNoPeers) {
					status = 503
				}
				if errors.Is(err, ErrCacheFull) || errors.Is(err, ErrQueuesFull) || errors.Is(err, ErrAdmissionExpired) {
					status = 429
				}
				apiError(w, status, err.Error())
				return
			}
			jsonResponse(w, 202, result)
		default:
			apiError(w, 404, "not found")
		}
	})
}
