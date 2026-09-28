"""Shared output contract. Concrete CLI and TUI renderers stay in their hosts."""

from .contracts import Renderer
from .silent import SilentRenderer

__all__ = ["Renderer", "SilentRenderer"]
