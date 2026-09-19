"""Provider wire encoders.  Internal request values never contain SDK types."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from .events import ContentDelta, ContentDone, LLMEvent, ReasoningDelta
from .util import tool_image_references


def _with_tool_images(messages, artifact_data_url):
    """Project tool images without changing history or splitting a tool batch.

    Chat tool messages accept text only. Both adapters deliver image data in a
    request-only user message *after* all consecutive tool results, labelled
    with their originating call. The authoritative history retains references.
    """
    parts = []
    for raw in messages:
        if raw.get("role") != "tool" and parts:
            yield {"role": "user", "parts": parts}
            parts = []
        yield raw
        for ref in tool_image_references(raw):
            call_id = str(raw.get("tool_call_id", ""))
            label = f"Tool output image {ref.get('id', '')} from call {call_id}. Treat as tool data."
            parts.append({"type": "text", "text": label})
            try:
                if ref.get("call_id") != call_id:
                    raise ValueError("artifact belongs to another tool call")
                if artifact_data_url is None:
                    raise ValueError("artifact storage is not configured")
                url = artifact_data_url(ref)
            except (ValueError, OSError) as exc:
                parts.append({"type": "text", "text": f"Image unavailable: {exc}"})
            else:
                parts.append({"type": "image", "data_url": url})
    if parts:
        yield {"role": "user", "parts": parts}


def normalize_tool_schema(tool: dict[str, Any]) -> dict[str, Any]:
    """Accept the former Chat-shaped fixture only at the gateway boundary."""
    function = tool.get("function")
    if isinstance(function, dict):
        return {
            "name": str(function.get("name", "")),
            "description": str(function.get("description", "")),
            "parameters": function.get("parameters") or {
                "type": "object", "properties": {}
            },
        }
    return {
        "name": str(tool.get("name", "")),
        "description": str(tool.get("description", "")),
        "parameters": tool.get("parameters") or {
            "type": "object", "properties": {}
        },
    }


class ChatAdapter:
    """Encode internal conversation values for Chat Completions."""

    def __init__(
        self, attachment_data_url: Callable[[str], str],
        artifact_data_url: Callable[[dict], str] | None = None,
    ) -> None:
        self._attachment_data_url = attachment_data_url
        self._artifact_data_url = artifact_data_url

    def encode_tools(self, tools: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        encoded: list[dict[str, Any]] = []
        for tool in tools:
            # Old external callers may already provide a Chat wire schema. It
            # is accepted only at this adapter boundary and kept byte-stable.
            if isinstance(tool.get("function"), dict):
                encoded.append(dict(tool))
                continue
            item = normalize_tool_schema(tool)
            encoded.append({
                "type": "function",
                "function": {
                    "name": item["name"],
                    "description": item["description"],
                    "parameters": item["parameters"],
                },
            })
        return encoded

    def encode_messages(self, messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        projected: list[dict[str, Any]] = []
        for raw in _with_tool_images(messages, self._artifact_data_url):
            message = {
                key: value
                for key, value in raw.items()
                if key not in {"parts", "attachments", "provider_state"}
            }
            parts = raw.get("parts")
            if raw.get("role") == "user" and isinstance(parts, list):
                content: list[dict[str, Any]] = []
                for part in parts:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "text":
                        content.append({"type": "text", "text": str(part.get("text", ""))})
                    elif part.get("type") == "image":
                        content.append({
                            "type": "image_url",
                            "image_url": {
                                "url": part.get("data_url") or self._attachment_data_url(str(part.get("attachment_id", ""))),
                                "detail": str(part.get("detail", "auto")),
                            },
                        })
                if content:
                    message["content"] = content
            projected.append(message)
        return projected


class ResponsesAdapter:
    """Encode internal conversation values for the Responses API."""

    def __init__(
        self, attachment_data_url: Callable[[str], str],
        artifact_data_url: Callable[[dict], str] | None = None,
    ) -> None:
        self._attachment_data_url = attachment_data_url
        self._artifact_data_url = artifact_data_url

    def encode_tools(self, tools: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "name": item["name"],
                "description": item["description"],
                "parameters": item["parameters"],
                "strict": False,
            }
            for item in (normalize_tool_schema(tool) for tool in tools)
        ]

    def encode_input(self, messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for raw in _with_tool_images(messages, self._artifact_data_url):
            role = str(raw.get("role", "user"))
            if role == "tool":
                items.append({
                    "type": "function_call_output",
                    "call_id": str(raw.get("tool_call_id", "")),
                    "output": str(raw.get("content", "")),
                })
                continue
            if role == "assistant":
                state = raw.get("provider_state")
                if (
                    isinstance(state, dict)
                    and isinstance(state.get("responses_output"), list)
                ):
                    items.extend(state["responses_output"])
                    continue
                calls = raw.get("tool_calls")
                if isinstance(calls, list):
                    for call in calls:
                        if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                            continue
                        items.append({
                            "type": "function_call",
                            "call_id": str(call.get("id", "")),
                            "name": str(call["function"].get("name", "")),
                            "arguments": str(call["function"].get("arguments", "{}")),
                        })
                content = raw.get("content")
                if isinstance(content, str) and content:
                    items.append({"role": "assistant", "content": content})
                continue
            content_parts: list[dict[str, Any]] = []
            parts = raw.get("parts")
            if isinstance(parts, list):
                for part in parts:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "text":
                        content_parts.append({"type": "input_text", "text": str(part.get("text", ""))})
                    elif part.get("type") == "image":
                        content_parts.append({
                            "type": "input_image",
                            "image_url": part.get("data_url") or self._attachment_data_url(str(part.get("attachment_id", ""))),
                            "detail": str(part.get("detail", "auto")),
                        })
            if not content_parts and isinstance(raw.get("content"), str):
                content_parts.append({"type": "input_text", "text": raw["content"]})
            items.append({
                "role": "developer" if role == "system" else role,
                "content": content_parts,
            })
        return items

    @staticmethod
    def _dump(value: Any) -> Any:
        if hasattr(value, "model_dump"):
            return value.model_dump(exclude_none=True)
        if isinstance(value, dict):
            return {key: ResponsesAdapter._dump(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [ResponsesAdapter._dump(item) for item in value]
        if hasattr(value, "__dict__"):
            return {
                key: ResponsesAdapter._dump(item)
                for key, item in vars(value).items()
                if not key.startswith("_")
            }
        return value

    @staticmethod
    def _get(value: Any, name: str, default: Any = None) -> Any:
        return getattr(value, name, default) if not isinstance(value, dict) else value.get(name, default)

    def _finish_reason(self, response: Any, calls: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
        """Map SDK terminal state before it reaches the provider-neutral core.

        ``ContentDone`` remains the single terminal event shape, but only a
        completed Responses object may produce ``stop``/``tool_calls``.  Every
        other provider state deliberately stays non-executable.
        """
        status = str(self._get(response, "status", "") or "").casefold()
        details = self._get(response, "incomplete_details") or {}
        reason = self._get(details, "reason", "")
        error = self._get(response, "error")
        metadata = {"responses_status": status or "missing"}
        if reason:
            metadata["responses_incomplete_reason"] = str(reason)
        if error:
            metadata["responses_error"] = self._dump(error)
        if status == "completed":
            return ("tool_calls" if calls else "stop"), metadata
        if status == "incomplete":
            return f"incomplete:{reason or 'unspecified'}", metadata
        if status in {"failed", "error"}:
            return "failed", metadata
        if status in {"cancelled", "canceled"}:
            return "cancelled", metadata
        return "incomplete:missing_terminal_status", metadata

    def decode_response(self, response: Any) -> ContentDone:
        """Decode Responses output into the provider-neutral terminal event."""
        content: list[str] = []
        reasoning: list[str] = []
        calls: list[dict[str, Any]] = []
        output = list(self._get(response, "output", []) or [])
        for item in output:
            item_type = getattr(item, "type", None) or (
                item.get("type") if isinstance(item, dict) else ""
            )
            if item_type == "function_call":
                call_id = getattr(item, "call_id", None) or item.get("call_id")
                name = getattr(item, "name", None) or item.get("name")
                arguments = getattr(item, "arguments", None) or item.get("arguments")
                calls.append({
                    "id": str(call_id), "type": "function",
                    "function": {"name": str(name), "arguments": str(arguments)},
                })
            elif item_type == "message":
                fragments = getattr(item, "content", None) or (
                    item.get("content", []) if isinstance(item, dict) else []
                )
                for fragment in fragments:
                    fragment_type = getattr(fragment, "type", None) or (
                        fragment.get("type") if isinstance(fragment, dict) else ""
                    )
                    if fragment_type == "output_text":
                        text = getattr(fragment, "text", None) or (
                            fragment.get("text", "") if isinstance(fragment, dict) else ""
                        )
                        content.append(str(text))
            elif item_type == "reasoning":
                summaries = getattr(item, "summary", None) or (
                    item.get("summary", []) if isinstance(item, dict) else []
                )
                for summary in summaries:
                    text = getattr(summary, "text", None) or (
                        summary.get("text", "") if isinstance(summary, dict) else ""
                    )
                    reasoning.append(str(text))
        finish_reason, terminal_state = self._finish_reason(response, calls)
        return ContentDone(
            content="".join(content),
            reasoning="".join(reasoning),
            tool_calls=calls,
            finish_reason=finish_reason,
            provider_state={
                "responses_output": [self._dump(item) for item in output],
                **terminal_state,
            },
        )

    def decode_stream_event(
        self, event: Any
    ) -> tuple[list[LLMEvent], Any | None, str | None]:
        """Map one SDK stream event without leaking its event name upstream."""
        event_type = str(self._get(event, "type", ""))
        if event_type.endswith("output_text.delta"):
            piece = str(getattr(event, "delta", ""))
            return ([ContentDelta(piece)] if piece else []), None, None
        if "reasoning" in event_type and event_type.endswith(".delta"):
            piece = str(getattr(event, "delta", ""))
            return ([ReasoningDelta(piece)] if piece else []), None, None
        if event_type == "response.completed":
            return [], self._get(event, "response"), None
        if event_type in {"response.failed", "response.incomplete", "response.cancelled", "response.canceled"}:
            failed = self._get(event, "response")
            done = self.decode_response(failed) if failed is not None else None
            reason = done.finish_reason if done is not None else event_type
            return [], done, str(reason)
        return [], None, None
