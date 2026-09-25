"""Backward compatibility shim for wright.tools.knowledge_tools."""

import sys
from ..infrastructure.tools.knowledge_tools import *

sys.modules[__name__] = sys.modules.get('wright.infrastructure.tools.knowledge_tools', sys.modules[__name__])
