"""Backward compatibility shim for wright.tools.episode_tools."""

import sys
from ..infrastructure.tools.episode_tools import *

sys.modules[__name__] = sys.modules.get('wright.infrastructure.tools.episode_tools', sys.modules[__name__])
