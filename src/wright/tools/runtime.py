"""Backward compatibility shim for wright.tools.runtime."""

import sys
from ..infrastructure.tools.runtime import *

sys.modules[__name__] = sys.modules.get('wright.infrastructure.tools.runtime', sys.modules[__name__])
