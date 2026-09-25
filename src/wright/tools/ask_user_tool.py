"""Backward compatibility shim for wright.tools.ask_user_tool."""

import sys
from ..infrastructure.tools.ask_user_tool import *

sys.modules[__name__] = sys.modules.get('wright.infrastructure.tools.ask_user_tool', sys.modules[__name__])
