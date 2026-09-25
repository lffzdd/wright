"""Backward-compatibility shim. Use wright.infrastructure.storage.attachments instead."""
import sys
from .infrastructure.storage import attachments

sys.modules[__name__] = attachments
