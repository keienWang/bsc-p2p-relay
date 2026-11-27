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
	config      *params.ChainConfig
	genesis     *types.Block
	chainIDText string // Immutable display value of the configured chain ID.
}

func (c *chainView) Config() *params.ChainConfig  { return c.config }
func (c *chainView) Genesis() *types.Block        { return c.genesis }
func (c *chainView) CurrentHeader() *types.Header { return c.genesis.Header() }
func genesisConstructor(name string) (func() *core.Genesis, error) {
	switch name {
	case "mainnet":
		return core.DefaultBSCGenesisBlock, nil
	case "testnet":
		return core.DefaultChapelGenesisBlock, nil
	default:
		return nil, fmt.Errorf("network must be mainnet or testnet")
	}
}

func chainFromGenesis(g *core.Genesis) *chainView {
	return newChainView(g.Config, g.ToBlock())
}

func network(name string) (*chainView, error) {
	genesis, err := genesisConstructor(name)
	if err != nil {
		return nil, err
	}
	return chainFromGenesis(genesis()), nil
}

// Network configuration is fixed for the lifetime of a chainView. Construct a
// new view when selecting another chain, rather than mutating its config.
func newChainView(config *params.ChainConfig, genesis *types.Block) *chainView {
	return &chainView{config: config, genesis: genesis, chainIDText: config.ChainID.String()}
}
