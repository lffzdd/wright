"""Provider-neutral conversation values kept in durable session state."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class TextPart:
    text: str
    type: Literal["text"] = "text"

    def to_dict(self) -> dict[str, str]:
        return {"type": self.type, "text": self.text}


@dataclass(frozen=True)
class ImagePart:
    attachment_id: str
    detail: Literal["auto", "low", "high"] = "auto"
    type: Literal["image"] = "image"

    def to_dict(self) -> dict[str, str]:
        return {
            "type": self.type,
            "attachment_id": self.attachment_id,
            "detail": self.detail,
        }


ConversationPart = TextPart | ImagePart


@dataclass(frozen=True)
class UserTurnInput:
    prompt: str
    attachment_ids: tuple[str, ...] = ()


@dataclass
class ConversationMessage:
    role: Literal["system", "developer", "user", "assistant", "tool"]
    parts: list[ConversationPart] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_call_id: str | None = None
    reasoning: str = ""
    provider_state: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "role": self.role,
            "content": "".join(part.text for part in self.parts if isinstance(part, TextPart)),
            "parts": [part.to_dict() for part in self.parts],
        }
        image_ids = [part.attachment_id for part in self.parts if isinstance(part, ImagePart)]
        if image_ids:
            value["attachments"] = image_ids
        if self.tool_calls:
            value["tool_calls"] = self.tool_calls
        if self.tool_call_id:
            value["tool_call_id"] = self.tool_call_id
        if self.reasoning:
            value["reasoning_content"] = self.reasoning
        if self.provider_state:
            value["provider_state"] = self.provider_state
        return value
