"""Bounded probes used by conditional scheduler triggers."""

from __future__ import annotations

import hashlib
import ipaddress
import socket
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_WEB_PROBE_BYTES = 1_000_000


def probe_public_web_page(url: str, *, timeout: float = 5.0) -> dict[str, Any]:
    """Return a stable page fingerprint while rejecting local/private targets."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("web trigger URL must use http or https")
    _reject_private_host(parsed.hostname, parsed.port)
    request = Request(
        url,
        headers={"User-Agent": "ReACTMulti-Scheduler/1.0"},
        method="GET",
    )
    opener = build_opener(_SafeRedirectHandler())
    try:
        response = opener.open(request, timeout=timeout)
    except HTTPError as exc:
        # HTTP status is monitored state, not a transport failure. Keep the
        # bounded response body in the fingerprint for 4xx/5xx changes.
        response = exc
    with response:
        final_url = response.geturl()
        final = urlparse(final_url)
        if not final.hostname:
            raise ValueError("web trigger redirect has no hostname")
        _reject_private_host(final.hostname, final.port)
        body = response.read(MAX_WEB_PROBE_BYTES + 1)
        if len(body) > MAX_WEB_PROBE_BYTES:
            raise ValueError(
                f"web trigger response exceeds {MAX_WEB_PROBE_BYTES} bytes"
            )
        return {
            "url": final_url,
            "status": int(getattr(response, "status", 200)),
            "etag": str(response.headers.get("ETag") or "")[:500],
            "last_modified": str(response.headers.get("Last-Modified") or "")[:500],
            "content_length": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        }


class _SafeRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlparse(newurl)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("web trigger redirect must use http or https")
        _reject_private_host(parsed.hostname, parsed.port)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _reject_private_host(hostname: str, port: int | None) -> None:
    try:
        addresses = socket.getaddrinfo(
            hostname,
            port or 443,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise ValueError(f"web trigger hostname cannot be resolved: {hostname}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise ValueError(
                f"web trigger rejects non-public address for {hostname}: {ip}"
            )
