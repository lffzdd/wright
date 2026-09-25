"""Backward compatibility shim for wright.tools.autonomy_tools."""

import sys
from ..infrastructure.tools.autonomy_tools import *

sys.modules[__name__] = sys.modules.get('wright.infrastructure.tools.autonomy_tools', sys.modules[__name__])
