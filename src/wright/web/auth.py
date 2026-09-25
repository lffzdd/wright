"""Backward compatibility shim for wright.web.auth."""

import sys
from ..interfaces.web import auth

sys.modules[__name__] = auth
