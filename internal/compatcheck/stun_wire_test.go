// SPDX-License-Identifier: GPL-3.0-or-later
package compatcheck

import (
	"encoding/binary"
	"fmt"
	"github.com/ethereum/go-ethereum/p2p/nat"
	"net"
	"testing"
	"time"
)

// Independently authored fixture uses only RFC 5389 bytes and the Go standard library at the
// server, so it cannot share a Pion encoder bug with the implementation.
func TestBSCSTUNIndependentWire(t *testing.T) {
	for _, mode := range []string{"valid", "no_attribute", "truncated_xor", "wrong_id_then_valid"} {
		t.Run(mode, func(t *testing.T) {
			sock, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
			if err != nil {
				t.Fatal(err)
			}
			defer sock.Close()
			_ = sock.SetDeadline(time.Now().Add(3 * time.Second))
			outcome := make(chan error, 1)
			go func() {
				data := make([]byte, 2048)
				length, peer, err := sock.ReadFromUDP(data)
				if err != nil {
					outcome <- err
					return
				}
				if length < 20 || binary.BigEndian.Uint16(data[:2]) != 1 || binary.BigEndian.Uint32(data[4:8]) != 0x2112a442 {
					outcome <- fmt.Errorf("invalid binding request")
					return
				}
				response := make([]byte, 32)
				binary.BigEndian.PutUint16(response[:2], 0x0101)
				binary.BigEndian.PutUint16(response[2:4], 12)
				copy(response[4:20], data[4:20])
				binary.BigEndian.PutUint16(response[20:22], 0x0020)
				binary.BigEndian.PutUint16(response[22:24], 8)
				response[25] = 1
				binary.BigEndian.PutUint16(response[26:28], 54321^0x2112)
				binary.BigEndian.PutUint32(response[28:32], 0xcb007107^0x2112a442)
				switch mode {
				case "no_attribute":
					response = response[:20]
					binary.BigEndian.PutUint16(response[2:4], 0)
				case "truncated_xor":
					response = response[:28]
					binary.BigEndian.PutUint16(response[2:4], 8)
					binary.BigEndian.PutUint16(response[22:24], 4)
				case "wrong_id_then_valid":
					bad := append([]byte(nil), response...)
					bad[19] ^= 1
					binary.BigEndian.PutUint32(bad[28:32], 0xcb007142^0x2112a442)
					if _, err = sock.WriteToUDP(bad, peer); err != nil {
						outcome <- err
						return
					}
					time.Sleep(20 * time.Millisecond)
				}
				_, err = sock.WriteToUDP(response, peer)
				outcome <- err
			}()
			mapper, err := nat.Parse("stun:" + sock.LocalAddr().String())
			if err != nil {
				t.Fatal(err)
			}
			ip, err := mapper.ExternalIP()
			if mode == "no_attribute" || mode == "truncated_xor" {
				if err == nil || ip != nil {
					t.Fatalf("malformed response accepted: %v %v", ip, err)
				}
			} else if err != nil || !ip.Equal(net.IPv4(203, 0, 113, 7)) {
				t.Fatalf("wire mismatch: %v %v", ip, err)
			}
			if err := <-outcome; err != nil {
				t.Fatal(err)
			}
		})
	}
}
