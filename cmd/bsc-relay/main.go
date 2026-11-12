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
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/keienWang/bsc-p2p-relay/internal/relay"
)

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
	cfg.Peers = peers
	if err := relay.LoopbackAddress(*api); err != nil {
		return err
	}
	node, err := relay.New(cfg)
	if err != nil {
		return err
	}
	defer node.Close()
	// Reserve the local API port before starting P2P. A bind failure must not
	// briefly start discovery/static dialing or emit a successful startup log.
	listener, err := net.Listen("tcp", *api)
	if err != nil {
		return err
	}
	defer listener.Close()
	if err = node.Start(); err != nil {
		return err
	}
	srv := &http.Server{Addr: *api, Handler: node.Handler(), ReadHeaderTimeout: 3 * time.Second, ReadTimeout: 5 * time.Second, WriteTimeout: 5 * time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 8192}
	errs := make(chan error, 1)
	go func() { errs <- srv.Serve(listener) }()
	slog.Info("experimental relay started; no chain/state verification or confirmations", "network", cfg.Network, "api", *api, "enode", node.Enode())
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	select {
	case err := <-errs:
		if !errors.Is(err, http.ErrServerClosed) {
			return err
		}
	case <-ctx.Done():
	}
	shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	return srv.Shutdown(shutdown)
}
