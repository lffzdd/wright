"""Egress proxy: host decisions, lookups, pinning, SNI and revocation (no sandbox)."""

import socket
import ssl
import struct
import threading
import time

import pytest

from wright.infrastructure.runtime.egress_proxy import _tls_server_name, open_tcp_lease

PUBLIC = "93.184.215.14"


class Decider:
    def __init__(self, allowed=(), *, private_ok=False):
        self.allowed = set(allowed)
        self.private_ok = private_ok
        self.calls = []
        self.revoked = False

    def decide(self, host, port, *, private):
        self.calls.append((host, port, private))
        return host in self.allowed and (not private or self.private_ok)

    def still_allowed(self, host, port, *, private):
        return not self.revoked and host in self.allowed


@pytest.fixture
def echo():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    received = []

    def serve():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return

            def handle(conn=conn):
                with conn:
                    while data := conn.recv(65536):
                        received.append(data)
                        conn.sendall(data)
            threading.Thread(target=handle, daemon=True).start()
    threading.Thread(target=serve, daemon=True).start()
    yield server.getsockname()[1], received
    server.close()


@pytest.fixture
def proxy(echo):
    port, _ = echo
    connected, lookups, leases = [], [], []

    def make(decider, *, addresses=(PUBLIC,)):
        def resolver(host, _port):
            lookups.append(host)
            return list(addresses)

        def connector(address, _port, timeout):
            connected.append(address)
            return socket.create_connection(("127.0.0.1", port), timeout)
        lease = open_tcp_lease(decider, resolver=resolver, connector=connector)
        leases.append(lease)
        return lease
    make.connected, make.lookups = connected, lookups
    yield make
    for lease in leases:
        lease.close()


def client(lease):
    sock = socket.create_connection(("127.0.0.1", lease.port), timeout=5)
    sock.settimeout(5)
    return sock


def connect(lease, authority):
    sock = client(lease)
    sock.sendall(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode())
    return sock, read_head(sock)


def read_head(sock):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


def client_hello(server_name):
    context = ssl.create_default_context()
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = context.wrap_bio(incoming, outgoing, server_hostname=server_name)
    with pytest.raises(ssl.SSLWantReadError):
        tls.do_handshake()
    return outgoing.read()


def closed(sock, timeout=5.0):
    sock.settimeout(timeout)
    try:
        return sock.recv(1) == b""
    except (ConnectionResetError, BrokenPipeError):
        return True


def test_allowed_connect_tunnels_to_the_address_that_was_checked(proxy):
    lease = proxy(Decider({"pypi.org"}))
    sock, head = connect(lease, "pypi.org:443")
    assert head.startswith(b"HTTP/1.1 200")
    sock.sendall(b"SSH-2.0-not-tls\r\n")
    assert sock.recv(100) == b"SSH-2.0-not-tls\r\n"
    assert proxy.connected == [PUBLIC] and proxy.lookups == ["pypi.org"]


def test_denied_hosts_are_never_resolved_and_are_reported(proxy):
    decider = Decider()
    lease = proxy(decider)
    _sock, head = connect(lease, "secret-value.attacker.example:443")
    assert head.startswith(b"HTTP/1.1 403")
    assert proxy.lookups == [] and proxy.connected == []
    assert lease.denials() == [{"host": "secret-value.attacker.example", "port": 443, "reason": "not allowed"}]
    connect(lease, "secret-value.attacker.example:443")
    assert len(lease.denials()) == 1


def test_names_resolving_privately_are_decided_again_as_private(proxy):
    decider = Decider({"intranet.example"})
    lease = proxy(decider, addresses=("10.0.0.7",))
    _, head = connect(lease, "intranet.example:443")
    assert head.startswith(b"HTTP/1.1 403")
    assert decider.calls == [("intranet.example", 443, False), ("intranet.example", 443, True)]
    assert lease.denials()[0]["reason"] == "resolves to a local or private address"
    trusting = proxy(Decider({"intranet.example"}, private_ok=True), addresses=("10.0.0.7",))
    _, head = connect(trusting, "intranet.example:443")
    assert head.startswith(b"HTTP/1.1 200")


def test_private_literals_are_decided_as_private_without_lookup(proxy):
    decider = Decider({"localhost"})
    lease = proxy(decider)
    _, head = connect(lease, "localhost:8080")
    assert head.startswith(b"HTTP/1.1 403") and decider.calls == [("localhost", 8080, True)] and proxy.lookups == []


def test_plain_http_is_forwarded_in_origin_form_and_closes(proxy, echo):
    _, _received = echo
    lease = proxy(Decider({"example.com"}))
    sock = client(lease)
    sock.sendall(b"GET http://example.com/a?b=1 HTTP/1.1\r\nHost: example.com\r\nProxy-Connection: keep-alive\r\n\r\n")
    echoed = read_head(sock)
    assert echoed.startswith(b"GET /a?b=1 HTTP/1.1\r\n")
    assert b"Proxy-Connection" not in echoed and b"Connection: close" in echoed


def test_plain_http_host_header_must_match_the_target(proxy):
    lease = proxy(Decider({"example.com", "other.example"}))
    sock = client(lease)
    sock.sendall(b"GET http://example.com/ HTTP/1.1\r\nHost: other.example\r\n\r\n")
    assert read_head(sock).startswith(b"HTTP/1.1 400")
    assert proxy.connected == []


def test_https_urls_sent_in_the_clear_and_origin_form_are_rejected(proxy):
    lease = proxy(Decider({"example.com"}))
    for line in (b"GET https://example.com/ HTTP/1.1", b"GET / HTTP/1.1"):
        sock = client(lease)
        sock.sendall(line + b"\r\nHost: example.com\r\n\r\n")
        assert read_head(sock).startswith(b"HTTP/1.1 400")


def socks(lease, host, port):
    sock = client(lease)
    sock.sendall(b"\x05\x01\x00")
    assert sock.recv(2) == b"\x05\x00"
    name = host.encode()
    sock.sendall(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + struct.pack("!H", port))
    return sock, sock.recv(10)


def test_socks5_uses_the_same_host_policy(proxy):
    lease = proxy(Decider({"github.com"}))
    sock, reply = socks(lease, "github.com", 22)
    assert reply[1] == 0
    sock.sendall(b"ping")
    assert sock.recv(4) == b"ping"
    _, reply = socks(lease, "gitlab.com", 22)
    assert reply[1] == 0x02
    assert {"host": "gitlab.com", "port": 22, "reason": "not allowed"} in lease.denials()


def test_tls_server_name_must_equal_the_allowed_host(proxy, echo):
    _, _received = echo
    lease = proxy(Decider({"pypi.org"}))
    sock, _ = connect(lease, "pypi.org:443")
    hello = client_hello("pypi.org")
    sock.sendall(hello)
    assert sock.recv(5) == hello[:5]
    fronted, _ = connect(lease, "pypi.org:443")
    fronted.sendall(client_hello("evil.example"))
    assert closed(fronted)
    assert {"host": "pypi.org", "port": 443, "reason": "TLS server name differs from the allowed host"} in lease.denials()


def test_tls_without_server_name_is_refused_for_names_only(proxy):
    lease = proxy(Decider({"pypi.org", PUBLIC}))
    hello = client_hello(None)
    sock, _ = connect(lease, "pypi.org:443")
    sock.sendall(hello)
    assert closed(sock)
    literal, _ = connect(lease, f"{PUBLIC}:443")
    literal.sendall(hello)
    assert literal.recv(5) == hello[:5]


def test_fragmented_client_hello_is_reassembled():
    hello = client_hello("example.com")
    assert _tls_server_name(hello[:10]) == "incomplete"
    assert _tls_server_name(hello) == ("tls", "example.com")
    body = hello[5:]
    split = body[:40], body[40:]
    records = b"".join(b"\x16\x03\x01" + struct.pack("!H", len(part)) + part for part in split)
    assert _tls_server_name(records) == ("tls", "example.com")
    assert _tls_server_name(b"GET / HTTP/1.1\r\n") == "not_tls"


def test_revocation_closes_open_tunnels(proxy):
    decider = Decider({"pypi.org"})
    lease = proxy(decider)
    sock, _ = connect(lease, "pypi.org:443")
    sock.sendall(b"x")
    assert sock.recv(1) == b"x"
    decider.revoked = True
    assert closed(sock, timeout=4)
    assert {"host": "pypi.org", "port": 443, "reason": "permission revoked"} in lease.denials()


def test_closing_the_lease_ends_tunnels_and_refuses_new_connections(proxy):
    lease = proxy(Decider({"pypi.org"}))
    sock, _ = connect(lease, "pypi.org:443")
    lease.close()
    assert closed(sock)
    time.sleep(0.05)
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", lease.port), timeout=1)


def test_a_failing_decider_denies(proxy):
    class Broken(Decider):
        def decide(self, host, port, *, private):
            raise RuntimeError("store unreadable")
    lease = proxy(Broken({"pypi.org"}))
    sock = client(lease)
    sock.sendall(b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\n")
    assert read_head(sock).startswith(b"HTTP/1.1 403") and proxy.connected == []
