"""Backward compatibility shim for wright.tools.plan_tools."""

import sys
from ..infrastructure.tools.plan_tools import *

sys.modules[__name__] = sys.modules.get('wright.infrastructure.tools.plan_tools', sys.modules[__name__])
