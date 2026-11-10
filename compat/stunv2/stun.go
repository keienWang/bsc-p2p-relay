// SPDX-License-Identifier: GPL-3.0-or-later
// Package stun adapts the small STUN v2 API surface used by pinned BSC v1.8.2
// to maintained Pion STUN v3. It is NOT a complete STUN v2 implementation.
// All protocol and network work is delegated to the upstream v3 implementation.
package stun

import upstream "github.com/pion/stun/v3"

const DefaultPort = upstream.DefaultPort

type Client = upstream.Client
type Event = upstream.Event
type Message = upstream.Message
type Setter = upstream.Setter
type XORMappedAddress = upstream.XORMappedAddress

var TransactionID = upstream.TransactionID
var BindingRequest = upstream.BindingRequest

func Dial(network, address string) (*Client, error) { return upstream.Dial(network, address) }
func Build(setters ...Setter) (*Message, error)     { return upstream.Build(setters...) }
