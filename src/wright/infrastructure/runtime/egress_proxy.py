"""Per-command egress proxy for sandboxed Shell commands.

A sandboxed command has no network except one endpoint: this proxy's
listener for that command (a loopback port on macOS, a Unix socket bridged
into the network namespace on Linux). The proxy speaks HTTP CONNECT, plain
HTTP forwarding and SOCKS5, and controls only host and port:

* the host the client names is decided first; denied or unanswered names are
  never resolved, so the sandbox cannot leak data through DNS lookups;
* allowed names are resolved here, every address is checked, and the
  connection goes to an address that was checked (no second lookup);
* a CONNECT or SOCKS tunnel carrying TLS must present an SNI equal to the
  host that was allowed; TLS is never decrypted, so method and path stay
  invisible and are not part of the policy.

Any failure closes the client connection; nothing falls back to a direct
connection.
"""

from __future__ import annotations

import ipaddress
import os
import select
import socket
import struct
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from ...domain.gateway.runtime_protocols import EgressDecider
from ...domain.policy.permission.hosts import (
    format_authority,
    ip_literal,
    is_private_literal,
    is_public_address,
    normalize_host,
)
from .network import resolve_addresses

_HEADER_LIMIT = 64 * 1024
_HELLO_LIMIT = 64 * 1024
_HANDSHAKE_TIMEOUT = 30.0
_CONNECT_TIMEOUT = 30.0
_RECHECK_SECONDS = 1.0
_MAX_CONNECTIONS = 64
_MAX_DENIALS = 20

Connector = Callable[[str, int, float], socket.socket]


def _connect(address: str, port: int, timeout: float) -> socket.socket:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect((address, port))
        sock.settimeout(None)
        return sock
    except BaseException:
        sock.close()
        raise


@dataclass(frozen=True)
class _Target:
    host: str
    port: int


class EgressRefused(Exception):
    def __init__(self, reason: str, *, status: int = 403, socks: int = 0x02, target: _Target | None = None):
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.socks = socks
        self.target = target


class EgressLease:
    """One command's proxy listener; closing it ends every tunnel it carries."""

    def __init__(self, decider: EgressDecider, listener: socket.socket, *, port: int = 0, socket_path: Path | None = None,
                 resolver: Callable[[str, int], list[str]] | None = None, connector: Connector = _connect):
        self._decider = decider
        self._listener = listener
        self.port = port
        self.socket_path = socket_path
        self._resolver = resolver
        self._connector = connector
        self._lock = threading.Lock()
        self._open: set[socket.socket] = set()
        self._denials: list[dict[str, object]] = []
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._accept_loop, name="wright-egress", daemon=True)
        self._thread.start()

    # Lease surface -------------------------------------------------------

    def denials(self) -> list[dict[str, object]]:
        with self._lock:
            return [dict(item) for item in self._denials]

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self._listener.close()
        except OSError:
            pass
        with self._lock:
            sockets = list(self._open)
            self._open.clear()
        for sock in sockets:
            _close(sock)
        if self.socket_path is not None:
            try:
                self.socket_path.unlink()
            except OSError:
                pass

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    # Connections ---------------------------------------------------------

    def _accept_loop(self) -> None:
        while not self._closed.is_set():
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            with self._lock:
                if self._closed.is_set() or len(self._open) >= _MAX_CONNECTIONS:
                    _close(client)
                    continue
                self._open.add(client)
            threading.Thread(target=self._serve, args=(client,), name="wright-egress-conn", daemon=True).start()

    def _track(self, sock: socket.socket) -> bool:
        with self._lock:
            if self._closed.is_set():
                return False
            self._open.add(sock)
            return True

    def _release(self, *sockets: socket.socket | None) -> None:
        for sock in sockets:
            if sock is None:
                continue
            with self._lock:
                self._open.discard(sock)
            _close(sock)

    def _deny(self, target: _Target | None, reason: str) -> None:
        if target is None:
            return
        entry = {"host": target.host, "port": target.port, "reason": reason}
        with self._lock:
            if entry not in self._denials and len(self._denials) < _MAX_DENIALS:
                self._denials.append(entry)

    def _serve(self, client: socket.socket) -> None:
        upstream = None
        target = None
        try:
            client.settimeout(_HANDSHAKE_TIMEOUT)
            first = client.recv(1, socket.MSG_PEEK)
            if not first:
                return
            if first == b"\x05":
                target, upstream, pending = self._socks(client)
            else:
                target, upstream, pending = self._http(client)
            client.settimeout(None)
            self._relay(client, upstream, target, pending)
        except EgressRefused as refusal:
            self._deny(refusal.target or target, refusal.reason)
        except (OSError, ValueError):
            pass
        finally:
            self._release(client, upstream)

    def _ask(self, target: _Target, *, private: bool) -> bool:
        try:
            return bool(self._decider.decide(target.host, target.port, private=private))
        except Exception:
            return False

    def _open_upstream(self, target: _Target) -> socket.socket:
        literal_private = is_private_literal(target.host)
        if not self._ask(target, private=literal_private):
            raise _refused(target, "not allowed")
        literal = ip_literal(target.host)
        if literal is not None:
            addresses = [str(literal)]
        else:
            resolver = self._resolver or resolve_addresses
            try:
                addresses = resolver(target.host, target.port)
            except OSError as exc:
                raise _refused(target, "could not resolve", status=502, socks=0x04) from exc
            if not addresses:
                raise _refused(target, "could not resolve", status=502, socks=0x04)
        if not literal_private and any(not is_public_address(address) for address in addresses):
            # A public-looking name that resolves to a local or private
            # address is decided again as that kind of destination.
            if not self._ask(target, private=True):
                raise _refused(target, "resolves to a local or private address")
        last_error: OSError | None = None
        for address in addresses:
            try:
                upstream = self._connector(address, target.port, _CONNECT_TIMEOUT)
            except OSError as exc:
                last_error = exc
                continue
            if not self._track(upstream):
                _close(upstream)
                raise _refused(target, "proxy closed", status=503, socks=0x01)
            return upstream
        raise _refused(target, f"connection failed: {last_error}", status=502, socks=0x05)

    # HTTP CONNECT and plain HTTP ----------------------------------------

    def _http(self, client: socket.socket) -> tuple[_Target, socket.socket, bytes]:
        head, rest = _read_head(client)
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3 or not parts[2].startswith("HTTP/"):
            _respond(client, 400, "Bad Request")
            raise ValueError("malformed request line")
        method, uri, version = parts
        headers = [line for line in lines[1:] if line]
        if method.upper() == "CONNECT":
            target = _authority_target(uri)
            if target is None:
                _respond(client, 400, "Bad Request")
                raise ValueError("bad CONNECT authority")
            upstream = self._refuse_over_http(client, target)
            client.sendall(f"{version} 200 Connection Established\r\n\r\n".encode("latin-1"))
            return target, upstream, rest
        parsed = urlsplit(uri)
        if parsed.scheme.lower() != "http" or not parsed.hostname:
            # Origin-form requests, or https:// sent in the clear, are not proxy requests.
            _respond(client, 400, "Bad Request")
            raise ValueError("not an absolute http URI")
        try:
            port = parsed.port or 80
        except ValueError:
            _respond(client, 400, "Bad Request")
            raise
        host = normalize_host(parsed.hostname)
        if host is None:
            _respond(client, 400, "Bad Request")
            raise ValueError("bad host")
        target = _Target(host, port)
        forwarded = []
        for line in headers:
            name, _, value = line.partition(":")
            key = name.strip().lower()
            if key in {"proxy-authorization", "proxy-connection", "connection", "keep-alive"}:
                continue
            if key == "host" and _authority_target(value.strip(), default_port=80) != target:
                _respond(client, 400, "Bad Request")
                raise ValueError("Host header differs from the request target")
            forwarded.append(line)
        if not any(line.lower().startswith("host:") for line in forwarded):
            forwarded.append(f"Host: {format_authority(host, port) if port != 80 else host}")
        forwarded.append("Connection: close")
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        upstream = self._refuse_over_http(client, target)
        request = "\r\n".join([f"{method} {path} {version}", *forwarded, "", ""]).encode("latin-1")
        upstream.sendall(request + rest)
        # Plain HTTP carries no SNI; the upstream connection is already pinned.
        return target, upstream, b""

    def _refuse_over_http(self, client: socket.socket, target: _Target) -> socket.socket:
        try:
            return self._open_upstream(target)
        except EgressRefused as refusal:
            reason = "Wright blocked this host" if refusal.status == 403 else "Wright could not reach this host"
            _respond(client, refusal.status, reason, body=f"{reason}: {format_authority(target.host, target.port)} ({refusal.reason})\n")
            raise

    # SOCKS5 ---------------------------------------------------------------

    def _socks(self, client: socket.socket) -> tuple[_Target, socket.socket, bytes]:
        greeting = _recv_exact(client, 2)
        methods = _recv_exact(client, greeting[1])
        if 0x00 not in methods:
            client.sendall(b"\x05\xff")
            raise ValueError("SOCKS client offers no usable method")
        client.sendall(b"\x05\x00")
        version, command, _, address_type = _recv_exact(client, 4)
        if version != 5:
            raise ValueError("bad SOCKS version")
        if address_type == 0x01:
            raw_host = str(ipaddress.IPv4Address(_recv_exact(client, 4)))
        elif address_type == 0x04:
            raw_host = str(ipaddress.IPv6Address(_recv_exact(client, 16)))
        elif address_type == 0x03:
            raw_host = _recv_exact(client, _recv_exact(client, 1)[0]).decode("ascii", errors="replace")
        else:
            _socks_reply(client, 0x08)
            raise ValueError("unsupported SOCKS address type")
        port = struct.unpack("!H", _recv_exact(client, 2))[0]
        host = normalize_host(raw_host)
        if command != 0x01 or host is None or not port:
            _socks_reply(client, 0x07 if command != 0x01 else 0x04)
            raise ValueError("unsupported SOCKS request")
        target = _Target(host, port)
        try:
            upstream = self._open_upstream(target)
        except EgressRefused as refusal:
            _socks_reply(client, refusal.socks)
            raise
        _socks_reply(client, 0x00)
        return target, upstream, b""

    # Tunnel ---------------------------------------------------------------

    def _relay(self, client: socket.socket, upstream: socket.socket, target: _Target, pending: bytes) -> None:
        inspected = False
        buffered = pending
        sockets = [client, upstream]
        private = is_private_literal(target.host)
        while not self._closed.is_set():
            if not inspected and buffered:
                verdict = _tls_server_name(buffered)
                if verdict == "incomplete":
                    if len(buffered) >= _HELLO_LIMIT:
                        raise _refused(target, "TLS client hello is too large to inspect")
                else:
                    inspected = True
                    if isinstance(verdict, tuple):
                        name = normalize_host(verdict[1]) if verdict[1] else None
                        # A name must be presented and equal the allowed host;
                        # only an IP-literal target may omit it.
                        if name != target.host and not (name is None and ip_literal(target.host) is not None):
                            raise _refused(target, "TLS server name differs from the allowed host")
                    upstream.sendall(buffered)
                    buffered = b""
            readable, _, broken = select.select(sockets, [], sockets, _RECHECK_SECONDS)
            if broken:
                return
            if not readable:
                try:
                    allowed = self._decider.still_allowed(target.host, target.port, private=private)
                except Exception:
                    allowed = False
                if not allowed:
                    raise _refused(target, "permission revoked")
                continue
            for source in readable:
                data = source.recv(65536)
                if not data:
                    return
                if source is client and not inspected:
                    buffered += data
                elif source is client:
                    upstream.sendall(data)
                else:
                    # The server spoke first, so this is not a TLS client hello.
                    if not inspected:
                        inspected = True
                        if buffered:
                            upstream.sendall(buffered)
                            buffered = b""
                    client.sendall(data)


def _refused(target: _Target, reason: str, *, status: int = 403, socks: int = 0x02) -> EgressRefused:
    return EgressRefused(reason, status=status, socks=socks, target=target)


def open_tcp_lease(decider: EgressDecider, **options) -> EgressLease:
    """Loopback listener on an ephemeral port (macOS Seatbelt allows exactly it)."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(_MAX_CONNECTIONS)
    except BaseException:
        listener.close()
        raise
    return EgressLease(decider, listener, port=listener.getsockname()[1], **options)


def open_unix_lease(decider: EgressDecider, path: Path, **options) -> EgressLease:
    """Unix-socket listener bridged into a Linux network namespace."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        listener.bind(str(path))
        os.chmod(path, 0o600)
        listener.listen(_MAX_CONNECTIONS)
    except BaseException:
        listener.close()
        raise
    return EgressLease(decider, listener, socket_path=path, **options)


# Wire helpers -----------------------------------------------------------------


def _close(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("connection closed")
        data += chunk
    return data


def _read_head(sock: socket.socket) -> tuple[bytes, bytes]:
    data = b""
    while b"\r\n\r\n" not in data:
        if len(data) > _HEADER_LIMIT:
            _respond(sock, 431, "Request Header Fields Too Large")
            raise ValueError("request head too large")
        chunk = sock.recv(8192)
        if not chunk:
            raise ConnectionError("connection closed")
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    return head, rest


def _respond(sock: socket.socket, status: int, reason: str, *, body: str = "") -> None:
    payload = body.encode("utf-8", errors="replace")
    try:
        sock.sendall((f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain; charset=utf-8\r\n"
                      f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n").encode("latin-1") + payload)
    except OSError:
        pass


def _socks_reply(sock: socket.socket, code: int) -> None:
    try:
        sock.sendall(bytes([5, code, 0, 1, 0, 0, 0, 0, 0, 0]))
    except OSError:
        pass


def _authority_target(authority: str, *, default_port: int | None = None) -> _Target | None:
    value = authority.strip()
    if value.startswith("["):
        host, _, rest = value[1:].partition("]")
        port_text = rest[1:] if rest.startswith(":") else ""
        if rest and not rest.startswith(":"):
            return None
    elif value.count(":") == 1:
        host, _, port_text = value.partition(":")
    else:
        host, port_text = value, ""
    if port_text:
        if not port_text.isdigit():
            return None
        port = int(port_text)
    elif default_port is not None:
        port = default_port
    else:
        return None
    normalized = normalize_host(host)
    if normalized is None or not 0 < port < 65536:
        return None
    return _Target(normalized, port)


def _tls_server_name(data: bytes) -> str | tuple[str, str | None]:
    """``("tls", sni)``, ``"incomplete"`` or ``"not_tls"`` for a client's first bytes."""
    if not data:
        return "incomplete"
    if data[0] != 0x16:
        return "not_tls"
    handshake = b""
    offset = 0
    while True:
        if len(data) < offset + 5:
            return "incomplete"
        if data[offset] != 0x16 or data[offset + 1] != 0x03:
            return "not_tls" if offset == 0 else ("tls", None)
        length = struct.unpack("!H", data[offset + 3:offset + 5])[0]
        if len(data) < offset + 5 + length:
            return "incomplete"
        handshake += data[offset + 5:offset + 5 + length]
        offset += 5 + length
        if len(handshake) >= 4:
            if handshake[0] != 0x01:
                return ("tls", None)
            body_length = int.from_bytes(handshake[1:4], "big")
            if len(handshake) >= 4 + body_length:
                return ("tls", _sni(handshake[4:4 + body_length]))


def _sni(hello: bytes) -> str | None:
    try:
        index = 2 + 32
        index += 1 + hello[index]
        index += 2 + struct.unpack("!H", hello[index:index + 2])[0]
        index += 1 + hello[index]
        if index + 2 > len(hello):
            return None
        end = index + 2 + struct.unpack("!H", hello[index:index + 2])[0]
        index += 2
        while index + 4 <= end:
            kind, size = struct.unpack("!HH", hello[index:index + 4])
            index += 4
            if kind == 0:
                names = hello[index:index + size]
                position = 2
                while position + 3 <= len(names):
                    name_type = names[position]
                    name_length = struct.unpack("!H", names[position + 1:position + 3])[0]
                    name = names[position + 3:position + 3 + name_length]
                    if name_type == 0:
                        return name.decode("ascii")
                    position += 3 + name_length
                return None
            index += size
    except (IndexError, struct.error, UnicodeDecodeError):
        return None
    return None


__all__ = ["EgressLease", "EgressRefused", "open_tcp_lease", "open_unix_lease"]
