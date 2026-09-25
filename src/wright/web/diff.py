"""Backward compatibility shim for wright.web.diff."""

import sys
from ..interfaces.web import diff

sys.modules[__name__] = diff
