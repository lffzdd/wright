"""Backward compatibility shim for wright.web.server."""

import sys
from ..interfaces.web import server

sys.modules[__name__] = server
