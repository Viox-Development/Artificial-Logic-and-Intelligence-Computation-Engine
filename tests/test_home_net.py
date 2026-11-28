"""Tests for the local network helpers in ``alice.home.net`` (#446).

Everything stays on localhost or is faked: no real subnet scan or UDP broadcast
leaves the test process.
"""

from __future__ import annotations

import socket

from alice.home import net


def _listener():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    return listener, listener.getsockname()[1]


def test_port_open_and_open_ports_use_a_real_localhost_listener():
    listener, port = _listener()
    try:
        # A single backlog slot is consumed by the first connect, so use a
        # separate listener for each probe.
        assert net.port_open("127.0.0.1", port) is True
    finally:
        listener.close()

    listener, port = _listener()
    try:
        assert net.open_ports("127.0.0.1", [port]) == [port]
    finally:
        listener.close()

    assert net.port_open("127.0.0.1", port) is False


def test_scan_port_keeps_only_hosts_whose_port_is_open(monkeypatch):
    monkeypatch.setattr(net, "port_open", lambda host, port, timeout: host == "192.168.1.2")
    assert net.scan_port(5555, ["192.168.1.2", "192.168.1.3"]) == ["192.168.1.2"]


def test_wake_on_lan_sends_the_magic_packet_to_both_ports(monkeypatch):
    sent: list[tuple[bytes, tuple[str, int]]] = []

    class FakeSocket:
        def __init__(self, *args, **kwargs):
            self.opts = []

        def setsockopt(self, *args):
            self.opts.append(args)

        def sendto(self, data, address):
            sent.append((data, address))

        def close(self):
            pass

    monkeypatch.setattr(net.socket, "socket", FakeSocket)
    net.wake_on_lan("aa:bb:cc:dd:ee:ff", broadcast="192.168.1.255")

    assert len(sent) == 2
    packet, address = sent[0]
    assert len(packet) == 102
    assert packet.startswith(b"\xff" * 6)
    assert packet[6:12] == bytes.fromhex("aabbccddeeff")
    assert address == ("192.168.1.255", 9)
    assert sent[1][1] == ("192.168.1.255", 7)


def test_local_ip_falls_back_to_loopback_without_network(monkeypatch):
    class NoNetworkSocket:
        def __init__(self, *args, **kwargs):
            pass

        def connect(self, address):
            raise OSError("no route")

        def close(self):
            pass

    monkeypatch.setattr(net.socket, "socket", NoNetworkSocket)
    assert net.local_ip() == "127.0.0.1"


def test_hostname_returns_empty_string_on_lookup_failure(monkeypatch):
    def fail(ip):
        raise OSError("not found")

    monkeypatch.setattr(net.socket, "gethostbyaddr", fail)
    assert net._hostname("192.0.2.1") == ""
