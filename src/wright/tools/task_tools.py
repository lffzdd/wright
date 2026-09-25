"""Backward compatibility shim for wright.tools.task_tools."""

import sys
from ..infrastructure.tools.task_tools import *

sys.modules[__name__] = sys.modules.get('wright.infrastructure.tools.task_tools', sys.modules[__name__])
