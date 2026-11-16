"""Host names and addresses as network rules see them.

One normalization serves Shell egress (CONNECT/SOCKS host names) and the
HTTP tools (URL hosts), so a host rule names the same thing for both.
"""

from __future__ import annotations

import ipaddress
import re

_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_PRIVATE_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"})


def normalize_host(raw: str) -> str | None:
    """Canonical lowercase ASCII host, or None when it is not a plain host.

    IP literals keep their compressed form; IPv6 brackets are removed.
    Names are IDNA-encoded, trailing dots dropped, and userinfo, ports,
    wildcards and empty labels are rejected.
    """

    if not isinstance(raw, str):
        return None
    host = raw.strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not host or len(host) > 253 or any(char in host for char in "/@?#*\\ \t\r\n%"):
        return None
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    host = host.rstrip(".")
    try:
        host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    labels = host.split(".")
    if not host or any(not _LABEL.match(label) for label in labels):
        return None
    if all(label.isdigit() for label in labels):
        # ``2130706433`` or ``127.1`` style numerals reach real addresses on
        # some resolvers; they are not names a rule may vouch for.
        return None
    return host


def ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def is_public_address(address: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether an address is a globally routable unicast destination."""

    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped or ip.sixtofour
        if mapped is not None:
            ip = mapped
        elif ip in _NAT64:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip in _FAKE_IP:
        return True
    return bool(ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_unspecified or ip.is_loopback or ip.is_link_local))


_NAT64 = ipaddress.ip_network("64:ff9b::/96")
# The benchmarking range: "fake-IP" DNS of local proxy and VPN clients answers
# every public name from it, so treating it as private would block everything.
# Real services there are rare; it is never a loopback or metadata address.
_FAKE_IP = ipaddress.ip_network("198.18.0.0/15")


def is_private_literal(host: str) -> bool:
    """Hosts that name a local or private destination without any lookup."""

    if host in _PRIVATE_NAMES or host.endswith(".localhost"):
        return True
    ip = ip_literal(host)
    return ip is not None and not is_public_address(ip)


def host_matches(pattern: str, host_match: str, host: str) -> bool:
    """``exact`` compares names; ``subdomains`` matches strict subdomains only."""

    if not pattern or not host:
        return False
    if host_match == "subdomains":
        return ip_literal(host) is None and host.endswith("." + pattern)
    return host == pattern


def format_authority(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
