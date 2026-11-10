// SPDX-License-Identifier: GPL-3.0-or-later
package compatcheck

import (
	"encoding/binary"
	"fmt"
	"net"
	"testing"
	"time"

	"github.com/ethereum/go-ethereum/p2p/nat"
	adapter "github.com/pion/stun/v2"
)

// Exercise the exact BSC caller. Response bytes are independently encoded from
// RFC 8489 sections 6/14.2, not constructed or decoded by Pion on the server.
func TestBSCSTUNAdapterLoopback(t *testing.T) {
	for _, mode := range []string{"valid", "missing_xor", "invalid_family"} {
		t.Run(mode, func(t *testing.T) { testBSCSTUNLoopback(t, mode) })
	}
}
func udpServer(t *testing.T) *net.UDPConn {
	t.Helper()
	s, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { s.Close() })
	if err := s.SetDeadline(time.Now().Add(3 * time.Second)); err != nil {
		t.Fatal(err)
	}
	return s
}
func testBSCSTUNLoopback(t *testing.T, mode string) {
	server := udpServer(t)
	serverErr := make(chan error, 1)
	go func() {
		buffer := make([]byte, 2048)
		length, addr, err := server.ReadFromUDP(buffer)
		if err != nil {
			serverErr <- err
			return
		}
		if length < 20 || binary.BigEndian.Uint16(buffer[:2]) != 0x0001 || binary.BigEndian.Uint32(buffer[4:8]) != 0x2112a442 {
			serverErr <- fmt.Errorf("not an RFC 8489 binding request")
			return
		}
		size := 32
		if mode == "missing_xor" {
			size = 20
		}
		response := make([]byte, size)
		binary.BigEndian.PutUint16(response[:2], 0x0101) // Binding success.
		binary.BigEndian.PutUint16(response[2:4], uint16(size-20))
		binary.BigEndian.PutUint32(response[4:8], 0x2112a442)
		copy(response[8:20], buffer[8:20]) // Echo transaction ID, not a new random ID.
		if size == 32 {
			binary.BigEndian.PutUint16(response[20:22], 0x0020) // XOR-MAPPED-ADDRESS.
			binary.BigEndian.PutUint16(response[22:24], 8)
			response[25] = 1 // IPv4; byte24 reserved zero.
			if mode == "invalid_family" {
				response[25] = 0xff
			}
			binary.BigEndian.PutUint16(response[26:28], uint16(54321)^0x2112)
			binary.BigEndian.PutUint32(response[28:32], uint32(0xcb007107)^0x2112a442) // 203.0.113.7.
		}
		_, err = server.WriteToUDP(response, addr)
		serverErr <- err
	}()
	mapper, err := nat.Parse("stun:" + server.LocalAddr().String())
	if err != nil {
		t.Fatal(err)
	}
	ip, err := mapper.ExternalIP()
	if mode == "valid" {
		if err != nil {
			t.Fatal(err)
		}
		if !ip.Equal(net.IPv4(203, 0, 113, 7)) {
			t.Fatalf("mapped address changed: %s", ip)
		}
	} else if err == nil {
		t.Fatal("invalid mapped address was accepted")
	}
	if err := <-serverErr; err != nil {
		t.Fatal(err)
	}
}
func TestSTUNAdapterNoResponseTimesOut(t *testing.T) {
	server := udpServer(t)
	client, err := adapter.Dial("udp4", server.LocalAddr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	client.SetRTO(2 * time.Millisecond)
	message, err := adapter.Build(adapter.TransactionID, adapter.BindingRequest)
	if err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() {
		var callbackErr error
		err := client.Do(message, func(e adapter.Event) { callbackErr = e.Error })
		if err != nil {
			done <- err
		} else {
			done <- callbackErr
		}
	}()
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("silent endpoint produced success")
		}
	case <-time.After(time.Second):
		t.Fatal("timeout did not terminate request")
	}
}

// Pion v2/v3 do not promise cancellation of an already-blocked Do on Close.
// BSC closes after synchronous Do returns. Preserve that upstream contract.
func TestSTUNAdapterCloseRejectsNewRequests(t *testing.T) {
	server := udpServer(t)
	client, err := adapter.Dial("udp4", server.LocalAddr().String())
	if err != nil {
		t.Fatal(err)
	}
	if err := client.Close(); err != nil {
		t.Fatal(err)
	}
	message, err := adapter.Build(adapter.TransactionID, adapter.BindingRequest)
	if err != nil {
		t.Fatal(err)
	}
	called := false
	if err := client.Do(message, func(e adapter.Event) { called = true }); err == nil {
		t.Fatal("closed client accepted new request")
	}
	if called {
		t.Fatal("closed client invoked unexpected callback")
	}
}
