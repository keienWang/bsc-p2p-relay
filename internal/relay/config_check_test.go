// SPDX-License-Identifier: GPL-3.0-or-later
package relay

import (
	"net"
	"strings"
	"testing"

	"github.com/ethereum/go-ethereum/crypto"
	"github.com/ethereum/go-ethereum/p2p/enode"
)

func TestValidateConfigAndNewShareChecks(t *testing.T) {
	key, err := crypto.HexToECDSA(strings.Repeat("0", 63) + "1")
	if err != nil {
		t.Fatal(err)
	}
	peer := enode.NewV4(&key.PublicKey, net.IPv4(127, 0, 0, 1), 30311, 30311).URLv4()
	cases := []struct {
		name    string
		change  func(*Config)
		wantErr string
		chainID string
		peers   int
		boots   bool
	}{
		{"default mainnet", func(*Config) {}, "", "56", 0, true},
		{"mainnet without discovery", func(c *Config) { c.NoDiscovery = true }, "", "56", 0, false},
		{"testnet explicit peer", func(c *Config) { c.Network, c.Peers = "testnet", []string{peer} }, "", "97", 1, false},
		{"testnet without peer", func(c *Config) { c.Network = "testnet" }, "testnet requires", "", 0, false},
		{"unknown network", func(c *Config) { c.Network = "invalid" }, "network must be", "", 0, false},
		{"invalid peer after valid one", func(c *Config) { c.Peers = []string{peer, "invalid"} }, "invalid peer:", "", 0, false},
		{"out of bounds", func(c *Config) { c.CacheTTL = 0 }, "invalid peer or cache limits", "", 0, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cfg := DefaultConfig()
			tc.change(&cfg)
			checked := ValidateConfig(cfg)
			n, err := New(cfg)
			if tc.wantErr != "" {
				if n != nil || checked == nil || err == nil || checked.Error() != err.Error() ||
					!strings.Contains(checked.Error(), tc.wantErr) {
					t.Fatalf("ValidateConfig = %v, New error = %v, want shared diagnostic %q", checked, err, tc.wantErr)
				}
				return
			}
			if checked != nil || err != nil {
				t.Fatalf("ValidateConfig = %v, New error = %v, want successful preparation", checked, err)
			}
			t.Cleanup(n.Close)
			// No node or peer is started. Inspect the resulting runtime wiring.
			if got := n.Health().ChainID; got != tc.chainID {
				t.Errorf("chain ID = %s, want %s", got, tc.chainID)
			}
			if len(n.server.StaticNodes) != tc.peers || (len(n.server.BootstrapNodes) != 0) != tc.boots {
				t.Errorf("configured peers=%d, bootstrap nodes=%d", len(n.server.StaticNodes), len(n.server.BootstrapNodes))
			}
			if tc.peers > 0 && n.server.StaticNodes[0].URLv4() != peer {
				t.Error("startup dropped the explicitly validated peer")
			}
		})
	}
}
