// SPDX-License-Identifier: GPL-3.0-or-later
package main

import (
	"errors"
	"flag"
	"io"
	"net"
	"os"
	"testing"
)

// An invalid API address is a second, independent startup guard: even without
// the positional-argument check, run returns before constructing a Node or
// opening listeners. Do not make these global flag/argv tests parallel.
func runWithInvalidAPI(t *testing.T, args []string) error {
	t.Helper()
	return runWithArguments(t, append([]string{"--api", "not-an-address", "--no-discovery", "--p2p", "127.0.0.1:0"}, args...))
}

// Each test owns the global flag/argv state until its cleanup completes.
func runWithArguments(t *testing.T, args []string) error {
	t.Helper()
	originalFlags, originalArgs := flag.CommandLine, os.Args
	t.Cleanup(func() {
		flag.CommandLine, os.Args = originalFlags, originalArgs
	})
	flag.CommandLine = flag.NewFlagSet("bsc-relay-test", flag.ContinueOnError)
	flag.CommandLine.SetOutput(io.Discard)
	os.Args = append([]string{"bsc-relay-test"}, args...)
	return run()
}

func occupiedLoopbackListener(t *testing.T) net.Listener {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = listener.Close() })
	return listener
}

func assertListenError(t *testing.T, err error, address string) {
	t.Helper()
	var listenError *net.OpError
	if !errors.As(err, &listenError) || listenError.Op != "listen" || listenError.Addr == nil || listenError.Addr.String() != address {
		t.Fatalf("run error = %v, want listen error for %s", err, address)
	}
}

func TestAPIBindFailurePrecedesP2PStart(t *testing.T) {
	api := occupiedLoopbackListener(t)
	p2p := occupiedLoopbackListener(t)
	// Both ports are owned by this test. The old order fails on P2P instead,
	// so neither version can successfully start P2P or dial an external peer.
	err := runWithArguments(t, []string{"--api", api.Addr().String(), "--p2p", p2p.Addr().String(), "--no-discovery"})
	assertListenError(t, err, api.Addr().String())
}

func TestP2PStartFailureReleasesReservedAPI(t *testing.T) {
	api := occupiedLoopbackListener(t)
	p2p := occupiedLoopbackListener(t)
	address := api.Addr().String()
	if err := api.Close(); err != nil {
		t.Fatal(err)
	}
	err := runWithArguments(t, []string{"--api", address, "--p2p", p2p.Addr().String(), "--no-discovery"})
	assertListenError(t, err, p2p.Addr().String())
	replacement, err := net.Listen("tcp", address)
	if err != nil {
		t.Fatalf("API listener retained after P2P startup failure: %v", err)
	}
	if err := replacement.Close(); err != nil {
		t.Fatal(err)
	}
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
