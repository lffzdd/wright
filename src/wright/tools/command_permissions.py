"""Backward compatibility shim for wright.tools.command_permissions."""

import sys
from ..infrastructure.tools.command_permissions import *

sys.modules[__name__] = sys.modules.get('wright.infrastructure.tools.command_permissions', sys.modules[__name__])
