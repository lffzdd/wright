"""Network authority enforced at the outbound request boundary."""
from __future__ import annotations

import socket
from collections.abc import Callable

import httpx

from ...domain.policy.permission.hosts import is_public_address, normalize_host
from ...domain.policy.permission.types import InvocationGrant

Resolver = Callable[[str, int], list[str]]


def request_target(method: str, url: str, params=None) -> str:
    request = httpx.Request(method, url, params=params)
    if request.url.scheme not in {'http', 'https'} or not request.url.host:
        raise PermissionError('A network target requires an HTTP(S) origin')
    return str(request.url.copy_with(username='', password='', fragment=None))


def resolve_addresses(host: str, port: int) -> list[str]:
    """Every address the system resolver returns for ``host``."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]).split('%')[0] for info in infos))


def ensure_public_destination(host: str, port: int, *, resolver: Resolver = resolve_addresses) -> None:
    """Refuse hosts that are or resolve to local, private or link-local addresses."""
    normalized = normalize_host(host)
    if normalized is None:
        raise PermissionError('The network target host is not a plain host name')
    try:
        addresses = resolver(normalized, port)
    except OSError as exc:
        raise PermissionError(f'Could not resolve {normalized}: {exc}') from exc
    private = [address for address in addresses if not is_public_address(address)]
    if not addresses or private:
        raise PermissionError(
            f'{normalized} resolves to a local or private address ({", ".join(private) or "none"}); '
            'allow it with a network host rule that includes private addresses'
        )


class AuthorizedNetwork:
    def __init__(self, grant: InvocationGrant, *, validator: Callable[[], None] | None = None, transport=None,
                 resolver: Resolver | None = None):
        self.grant = grant
        self._validator = validator
        self._transport = transport or httpx.request
        # An injected transport never reaches the system resolver unless asked.
        self._resolver = resolver if resolver is not None else (resolve_addresses if transport is None else None)

    def request(self, method: str, url: str, *, operation: str, params=None, **kwargs):
        method = method.upper()
        target = request_target(method, url, params)
        approved = next((
            item for item in self.grant.network_targets
            if item.url == target and item.method == method and item.operation == operation
        ), None)
        if operation not in self.grant.operations or approved is None:
            raise PermissionError('Network request differs from the approved target or method')
        if self._validator is not None:
            self._validator()
        if kwargs.pop('follow_redirects', False):
            raise PermissionError('Redirects require separate authorization')
        if not approved.allow_private and self._resolver is not None:
            parsed = httpx.URL(target)
            ensure_public_destination(parsed.host, parsed.port or (443 if parsed.scheme == 'https' else 80),
                                      resolver=self._resolver)
        return self._transport(method, url, params=params, follow_redirects=False, **kwargs)
