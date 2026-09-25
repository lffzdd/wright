"""Backward compatibility shim for wright.tools.skill_tools."""

import sys
from ..infrastructure.tools.skill_tools import *

sys.modules[__name__] = sys.modules.get('wright.infrastructure.tools.skill_tools', sys.modules[__name__])
