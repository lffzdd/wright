"""Backward compatibility shim for wright.tools.file_tools."""

import sys
from ..infrastructure.tools.file_tools import *

sys.modules[__name__] = sys.modules.get('wright.infrastructure.tools.file_tools', sys.modules[__name__])
