"""Application Data Transfer Objects (DTOs).

DTOs represent decoupled data packets crossing application and presentation boundaries.
"""

from __future__ import annotations

from .run_agent_request import RunAgentRequest
from .stream_event_dto import StreamEventDTO, StreamEventType

__all__ = [
    "RunAgentRequest",
    "StreamEventDTO",
    "StreamEventType",
]
