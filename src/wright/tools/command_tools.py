"""Backward compatibility shim for wright.tools.command_tools."""

import sys
from ..infrastructure.tools.command_tools import *

sys.modules[__name__] = sys.modules.get('wright.infrastructure.tools.command_tools', sys.modules[__name__])
