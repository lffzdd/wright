"""Backward compatibility shim for wright.tools.capabilities."""

import sys
from ..infrastructure.tools.capabilities import *

sys.modules[__name__] = sys.modules.get('wright.infrastructure.tools.capabilities', sys.modules[__name__])
