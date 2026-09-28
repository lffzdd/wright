"""Subscribe a terminal renderer after the application has opened a session.

Application assembly does not import a renderer. CLI and TUI call this.
"""

from __future__ import annotations

from typing import Any

from ...application.session.publisher import EventPublisher
from .contracts import Renderer
from .subscriber import RendererEventSubscriber


def attach_renderer(publisher: EventPublisher, renderer: Renderer, *, session: Any = None) -> str:
    provider = (lambda: session) if session is not None else None
    return publisher.add_listener(RendererEventSubscriber(renderer, session_provider=provider))
