"""Backward compatibility shim for wright.tools.validation."""

import sys
from ..infrastructure.tools.validation import *

sys.modules[__name__] = sys.modules.get('wright.infrastructure.tools.validation', sys.modules[__name__])
