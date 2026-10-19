"""Network authority enforced at the outbound request boundary."""
from __future__ import annotations

from collections.abc import Callable

import httpx

from ...domain.policy.permission.types import InvocationGrant


def request_target(method: str, url: str, params=None) -> str:
    request = httpx.Request(method, url, params=params)
    if request.url.scheme not in {'http', 'https'} or not request.url.host:
        raise PermissionError('A network target requires an HTTP(S) origin')
    return str(request.url.copy_with(username='', password='', fragment=None))


class AuthorizedNetwork:
    def __init__(self, grant: InvocationGrant, *, validator: Callable[[], None] | None = None, transport=None):
        self.grant = grant
        self._validator = validator
        self._transport = transport or httpx.request

    def request(self, method: str, url: str, *, operation: str, params=None, **kwargs):
        method = method.upper()
        target = request_target(method, url, params)
        if operation not in self.grant.operations or not any(
            item.url == target and item.method == method and item.operation == operation
            for item in self.grant.network_targets
        ):
            raise PermissionError('Network request differs from the approved target or method')
        if self._validator is not None:
            self._validator()
        if kwargs.pop('follow_redirects', False):
            raise PermissionError('Redirects require separate authorization')
        return self._transport(method, url, params=params, follow_redirects=False, **kwargs)
