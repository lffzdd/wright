"""Backward compatibility shim for wright.tools.memory_tools."""

import sys
from ..infrastructure.tools.memory_tools import *

sys.modules[__name__] = sys.modules.get('wright.infrastructure.tools.memory_tools', sys.modules[__name__])
