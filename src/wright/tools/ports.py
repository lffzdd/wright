"""Backward compatibility shim for wright.tools.ports."""

import sys
from ..infrastructure.tools.ports import *

sys.modules[__name__] = sys.modules.get('wright.infrastructure.tools.ports', sys.modules[__name__])
