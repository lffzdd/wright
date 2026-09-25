"""Backward compatibility shim for wright.tools.base."""

import sys
from ..infrastructure.tools.base import *

sys.modules[__name__] = sys.modules.get('wright.infrastructure.tools.base', sys.modules[__name__])
