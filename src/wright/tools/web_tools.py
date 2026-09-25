"""Backward compatibility shim for wright.tools.web_tools."""

import sys
from ..infrastructure.tools.web_tools import *

sys.modules[__name__] = sys.modules.get('wright.infrastructure.tools.web_tools', sys.modules[__name__])
