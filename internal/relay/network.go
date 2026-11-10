// SPDX-License-Identifier: GPL-3.0-or-later
package relay

import (
	"fmt"
	"github.com/ethereum/go-ethereum/core"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/params"
)

// chainView deliberately advertises genesis, not an unverified peer's head.
// Genesis construction uses only transient memory, never a persistent chain DB.
type chainView struct {
	config  *params.ChainConfig
	genesis *types.Block
}

func (c *chainView) Config() *params.ChainConfig  { return c.config }
func (c *chainView) Genesis() *types.Block        { return c.genesis }
func (c *chainView) CurrentHeader() *types.Header { return c.genesis.Header() }
func network(name string) (*chainView, error) {
	var g *core.Genesis
	switch name {
	case "mainnet":
		g = core.DefaultBSCGenesisBlock()
	case "testnet":
		g = core.DefaultChapelGenesisBlock()
	default:
		return nil, fmt.Errorf("network must be mainnet or testnet")
	}
	return &chainView{g.Config, g.ToBlock()}, nil
}
