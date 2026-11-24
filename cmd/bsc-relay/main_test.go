// SPDX-License-Identifier: GPL-3.0-or-later
package main

import (
	"context"
	"errors"
	"flag"
	"io"
	"net"
	"os"
	"testing"

	"github.com/keienWang/bsc-p2p-relay/internal/relay"
)

// An invalid API address is a second, independent startup guard: even without
// the positional-argument check, run returns before constructing a Node or
// opening listeners. Do not make these global flag/argv tests parallel.
func runWithInvalidAPI(t *testing.T, args []string) error {
	t.Helper()
	originalFlags, originalArgs := flag.CommandLine, os.Args
	t.Cleanup(func() {
		flag.CommandLine, os.Args = originalFlags, originalArgs
	})
	flag.CommandLine = flag.NewFlagSet("bsc-relay-test", flag.ContinueOnError)
	flag.CommandLine.SetOutput(io.Discard)
	os.Args = append([]string{"bsc-relay-test", "--api", "not-an-address", "--no-discovery", "--p2p", "127.0.0.1:0"}, args...)
	return run()
}

func TestRejectPositionalArgumentsBeforeStartup(t *testing.T) {
	cases := []struct {
		name string
		args []string
	}{
		{"plain", []string{"unrecognized-subcommand"}},
		{"before-network", []string{"unrecognized-subcommand", "--network", "testnet"}},
		{"after-network", []string{"--network", "testnet", "unexpected-value"}},
		{"separate-bool-value", []string{"--no-discovery", "false", "--network", "testnet"}},
		{"after-terminator", []string{"--", "--network", "testnet"}},
		{"single-dash", []string{"-", "--network", "testnet"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := runWithInvalidAPI(t, tc.args)
			const want = "unexpected positional arguments; use --help for supported flags"
			if err == nil || err.Error() != want {
				t.Fatalf("run error = %v, want %q", err, want)
			}
		})
	}
}

func TestFlagOnlyArgumentsReachAddressValidation(t *testing.T) {
	for _, args := range [][]string{
		nil,
		{"--network", "testnet"},
		{"--no-discovery=false"},
		{"--"},
	} {
		t.Run("flags-only", func(t *testing.T) {
			err := runWithInvalidAPI(t, args)
			var addressError *net.AddrError
			if !errors.As(err, &addressError) {
				t.Fatalf("run error = %v, want API address validation error", err)
			}
		})
	}
}

func TestRunRelaySkipsConstructionWhenCanceled(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	cfg := relay.DefaultConfig()
	cfg.MaxPeers = 0 // New would reject this; a canceled startup should not reach it.
	if err := runRelay(ctx, func() {}, cfg, "127.0.0.1:0", true, defaultHTTPDrainTimeout); err != nil {
		t.Fatalf("canceled startup returned %v", err)
	}
}
