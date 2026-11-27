// SPDX-License-Identifier: GPL-3.0-or-later
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"net/netip"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/keienWang/bsc-p2p-relay/internal/relay"
)

const defaultHTTPDrainTimeout = 5 * time.Second

type peersFlag []string

func (p *peersFlag) String() string     { return fmt.Sprint([]string(*p)) }
func (p *peersFlag) Set(v string) error { *p = append(*p, v); return nil }
func main() {
	if err := run(); err != nil {
		slog.Error("relay stopped", "error", err)
		os.Exit(1)
	}
}
func run() error {
	cfg := relay.DefaultConfig()
	var peers peersFlag
	flag.StringVar(&cfg.Network, "network", cfg.Network, "mainnet (56) or testnet (97)")
	flag.StringVar(&cfg.ListenAddr, "p2p", cfg.ListenAddr, "P2P TCP/UDP listen address")
	api := flag.String("api", "127.0.0.1:8787", "loopback HTTP listen address")
	var httpDrainTimeout time.Duration
	flag.DurationVar(&httpDrainTimeout, "http-drain-timeout", defaultHTTPDrainTimeout, "graceful HTTP drain timeout (1ms..30s)")
	var observeOnly, checkConfig bool
	flag.BoolVar(&observeOnly, "observe-only", false, "keep observation endpoints but disable HTTP transaction submission")
	flag.BoolVar(&checkConfig, "check-config", false, "validate configuration without binding listeners or dialing peers")
	flag.BoolVar(&cfg.NoDiscovery, "no-discovery", false, "disable discovery; use explicit static peers")
	flag.IntVar(&cfg.MaxPeers, "max-peers", cfg.MaxPeers, "maximum P2P peers (1..200)")
	flag.IntVar(&cfg.CacheCount, "cache-count", cfg.CacheCount, "maximum cached transactions")
	flag.IntVar(&cfg.CacheBytes, "cache-bytes", cfg.CacheBytes, "maximum cached transaction bytes")
	flag.DurationVar(&cfg.CacheTTL, "cache-ttl", cfg.CacheTTL, "transaction memory retention")
	flag.Var(&peers, "peer", "static enode URL; repeatable")
	flag.Parse()
	if flag.NArg() != 0 {
		return errors.New("unexpected positional arguments; use --help for supported flags")
	}
	if httpDrainTimeout < time.Millisecond || httpDrainTimeout > 30*time.Second {
		return errors.New("HTTP drain timeout must be 1ms..30s")
	}
	cfg.Peers = peers
	if err := relay.LoopbackAddress(*api); err != nil {
		return fmt.Errorf("validate HTTP API address: %w", err)
	}
	if checkConfig {
		// Check only numeric API ports, without service-name resolution or a bind.
		if _, err := netip.ParseAddrPort(*api); err != nil {
			return fmt.Errorf("check HTTP API address (numeric port required): %w", err)
		}
		if err := checkP2PListenAddress(cfg.ListenAddr); err != nil {
			return fmt.Errorf("check P2P listen address: %w", err)
		}
		if err := relay.ValidateConfig(cfg); err != nil {
			return fmt.Errorf("configure relay: %w", err)
		}
		_, err := fmt.Fprintln(os.Stdout, "configuration check passed; listeners and peer connectivity were not checked")
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	return runRelay(ctx, stop, cfg, *api, observeOnly, httpDrainTimeout)
}

// Check P2P address structure, IPv6 literal syntax, and numeric port bounds.
// Startup remains responsible for resolving names and opening listeners.
func checkP2PListenAddress(address string) error {
	if address == "" {
		return nil // No configured P2P TCP listener.
	}
	host, port, err := net.SplitHostPort(address)
	if err != nil {
		return err
	}
	// A host containing a colon cannot be a DNS name. Validate its literal
	// syntax now; a scoped address still needs a usable interface at startup.
	if strings.Contains(host, ":") {
		ip, err := netip.ParseAddr(host)
		if err != nil || !ip.Is6() {
			return errors.New("invalid P2P IPv6 literal")
		}
	}
	digits, negative := port, false
	if len(digits) != 0 && (digits[0] == '+' || digits[0] == '-') {
		negative = digits[0] == '-'
		digits = digits[1:]
	}
	if digits == "" {
		return nil // net accepts an empty port as port 0.
	}
	for _, digit := range digits {
		if digit < '0' || digit > '9' {
			return nil // A possible service name; do not resolve it offline.
		}
	}
	number, err := strconv.ParseUint(digits, 10, 16)
	if err != nil || (negative && number != 0) {
		return errors.New("P2P numeric port must be 0..65535")
	}
	return nil
}

func runRelay(ctx context.Context, stopSignals func(), cfg relay.Config, api string, observeOnly bool, drainTimeout time.Duration) error {
	return runRelayWithHTTPListener(ctx, stopSignals, cfg, api, observeOnly, drainTimeout, net.Listen)
}

func runRelayWithHTTPListener(ctx context.Context, stopSignals func(), cfg relay.Config, api string, observeOnly bool, drainTimeout time.Duration, listen func(string, string) (net.Listener, error)) error {
	if ctx.Err() != nil {
		return nil
	}
	node, err := relay.New(cfg)
	if err != nil {
		return fmt.Errorf("configure relay: %w", err)
	}
	defer node.Close()
	if ctx.Err() != nil {
		return nil
	}
	if err = node.Start(); err != nil {
		return fmt.Errorf("start P2P node: %w", err)
	}
	if ctx.Err() != nil {
		return nil
	}
	var handler http.Handler
	if observeOnly {
		handler = node.ObservationHandler()
	} else {
		handler = node.Handler()
	}
	srv := &http.Server{Addr: api, Handler: handler, ReadHeaderTimeout: 3 * time.Second, ReadTimeout: 5 * time.Second, WriteTimeout: 5 * time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 8192}
	// Bind synchronously so startup logs never announce an API whose listener
	// failed to open. Serve owns the listener; defer also covers early returns.
	listener, err := listen("tcp", api)
	if err != nil {
		return fmt.Errorf("bind HTTP API: %w", err)
	}
	defer listener.Close()
	// A signal can arrive while the synchronous bind is in progress. Do
	// not begin serving or announce a launch if cancellation is already
	// observable here. Later cancellation follows the normal HTTP drain.
	if ctx.Err() != nil {
		stopSignals()
		return nil
	}
	errs := make(chan error, 1)
	go func() { errs <- srv.Serve(listener) }()
	slog.Info("experimental relay started; no chain/state verification or confirmations", "network", cfg.Network, "api", listener.Addr().String(), "observe_only", observeOnly, "enode", node.Enode())
	select {
	case err := <-errs:
		if !errors.Is(err, http.ErrServerClosed) {
			// A terminal Serve error closes the listener, but accepted
			// connections can outlive it. Close them before tearing down P2P.
			serveErr := fmt.Errorf("serve HTTP API: %w", err)
			if closeErr := srv.Close(); closeErr != nil {
				return errors.Join(serveErr, fmt.Errorf("close HTTP API: %w", closeErr))
			}
			return serveErr
		}
	case <-ctx.Done():
		// A second signal may terminate the process if graceful shutdown stalls.
		stopSignals()
	}
	shutdown, cancel := context.WithTimeout(context.Background(), drainTimeout)
	defer cancel()
	if err := shutdownHTTP(shutdown, srv); err != nil {
		return fmt.Errorf("shut down HTTP API: %w", err)
	}
	return nil
}

// Shutdown stops listeners immediately but leaves active connections open when
// its context expires. Close them as the bounded drain's final cleanup.
func shutdownHTTP(ctx context.Context, srv *http.Server) error {
	if err := srv.Shutdown(ctx); err != nil {
		if closeErr := srv.Close(); closeErr != nil {
			return errors.Join(err, closeErr)
		}
		return err
	}
	return nil
}
