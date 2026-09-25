"""Backward compatibility shim for wright.tools.mcp_client."""

import sys
from ..infrastructure.tools.mcp_client import *

sys.modules[__name__] = sys.modules.get('wright.infrastructure.tools.mcp_client', sys.modules[__name__])
