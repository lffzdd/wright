"""Backward compatibility shim for wright.tools.tool_search."""

import sys
from ..infrastructure.tools.tool_search import *

sys.modules[__name__] = sys.modules.get('wright.infrastructure.tools.tool_search', sys.modules[__name__])
