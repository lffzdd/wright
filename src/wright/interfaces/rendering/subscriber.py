"""Project serializable UI events onto a Renderer.

This module does not create the application event channel. Hosts subscribe
after the application has opened one.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from ...application.session.events import UiEventEnvelope
from ..i18n import format_issues, present, t
from ...domain.model.tool import ToolCall, ToolResult
from .contracts import Renderer


class RendererEventSubscriber:
    """Project serializable UI events back into an existing terminal renderer.

    Root events update the main transcript. Child events stay out of that
    transcript and become delegation progress lines.
    """

    def __init__(
        self,
        renderer: Renderer,
        *,
        session_provider: Callable[[], Any] | None = None,
    ) -> None:
        self.renderer = renderer
        self._calls: dict[str, ToolCall] = {}
        self._session_provider = session_provider

    def __call__(self, event: UiEventEnvelope) -> None:
        payload = event.payload
        if _agent_depth(payload) > 0:
            self._project_child(event)
            return
        if event.type == "session.history_requested":
            self._render_history(payload)
            return
        if event.type == "reasoning.delta":
            self.renderer.on_reasoning_delta(str(payload.get("piece", "")))
        elif event.type == "content.delta":
            self.renderer.on_content_delta(str(payload.get("piece", "")))
        elif event.type == "content.final":
            self.renderer.on_final(payload.get("content"))
        elif event.type in {"tool.planned", "tool.awaiting_approval", "tool.running"}:
            call = ToolCall(
                name=str(payload.get("name", "tool")),
                arguments=dict(payload.get("arguments") or {}),
                id=str(payload.get("call_id", "")),
            )
            self._calls[call.id] = call
            if event.type == "tool.planned":
                self.renderer.on_tool_call(call)
            else:
                self.renderer.on_tool_phase(call, event.type.removeprefix("tool."))
        elif event.type == "tool.output":
            self.renderer.on_tool_output(
                str(payload.get("call_id", "")),
                str(payload.get("output", "")),
            )
        elif event.type == "tool.finished":
            call_id = str(payload.get("call_id", ""))
            call = self._calls.get(call_id) or ToolCall(
                name=str(payload.get("name", "tool")), arguments={}, id=call_id
            )
            result = ToolResult(
                ok=bool(payload.get("ok")),
                err=str(payload.get("err", "")),
                data=payload.get("data"),
            )
            self.renderer.on_tool_result(call, result)
        elif event.type == "usage.request":
            self.renderer.on_usage(
                payload.get("prompt_tokens"), payload.get("completion_tokens"),
                payload.get("total_tokens"), payload.get("context_limit"),
            )
        elif event.type == "usage.task":
            self.renderer.on_usage_summary(
                int(payload.get("prompt_tokens", 0)),
                int(payload.get("completion_tokens", 0)),
                int(payload.get("total_tokens", 0)),
            )
        elif event.type == "system.notice":
            kind = payload.get("kind")
            if kind == "completion_rejected":
                self.renderer.on_completion_rejected(payload.get("issues", ()))
            elif kind == "context_compact":
                self.renderer.on_context_compact(
                    int(payload.get("folded_count", 0)),
                    payload.get("prompt_tokens"),
                    payload.get("context_limit"),
                    float(payload.get("context_watermark", 0)),
                )
            else:
                self.renderer.on_system_notice(_notice_text(payload))
        elif event.type == "system.checkpoint_error":
            self.renderer.on_checkpoint_error(str(payload.get("error", "")))
        elif event.type == "task.updated":
            self.renderer.on_agent_event(dict(payload))
        elif event.type == "session.status_changed":
            if payload.get("status") == "model_turn_started":
                self.renderer.on_turn_begin()

    def _project_child(self, event: UiEventEnvelope) -> None:
        payload = event.payload
        depth = _agent_depth(payload)
        prefix = "    " * (depth - 1) + "│ "
        if event.type == "tool.planned":
            brief = json.dumps(payload.get("arguments") or {}, ensure_ascii=False)
            if len(brief) > 80:
                brief = brief[:77] + "..."
            name = payload.get("name", "tool")
            self.renderer.on_system_notice(
                t("agent.child.planned", prefix=prefix, depth=depth, name=name, brief=brief)
            )
        elif event.type == "tool.finished":
            name = payload.get("name", "tool")
            if payload.get("ok"):
                self.renderer.on_system_notice(
                    t("agent.child.ok", prefix=prefix, depth=depth, name=name)
                )
            else:
                self.renderer.on_system_notice(
                    t(
                        "agent.child.failed",
                        prefix=prefix,
                        depth=depth,
                        name=name,
                        error=payload.get("err", ""),
                    )
                )
        elif event.type == "content.final":
            text = payload.get("content")
            if payload.get("display_code"):
                text = present(
                    str(payload.get("display_code")),
                    payload.get("display_params") if isinstance(payload.get("display_params"), dict) else {},
                    fallback=text if isinstance(text, str) else "",
                )
            text = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
            if len(text) > 200:
                text = text[:197] + "..."
            self.renderer.on_system_notice(
                t("agent.child.final", prefix=prefix, depth=depth, text=text)
            )
        elif event.type == "system.notice" and payload.get("kind") == "completion_rejected":
            self.renderer.on_system_notice(
                t("agent.child.verification", prefix=prefix, detail=format_issues(payload.get("issues")))
            )
        elif event.type == "task.updated":
            self.renderer.on_agent_event(dict(payload))

    def _render_history(self, payload: dict[str, Any]) -> None:
        render = getattr(self.renderer, "render_session_history", None)
        session = self._session_provider() if self._session_provider is not None else None
        if not callable(render) or session is None:
            self.renderer.on_system_notice(t("history.above"))
            return
        max_turns = payload.get("max_turns", 5)
        pager = bool(payload.get("pager"))
        if pager:
            render(session, pager=True)
            return
        render(session, max_turns=int(max_turns) if isinstance(max_turns, int) else 5)


def _notice_text(payload: dict[str, Any]) -> str:
    text = str(payload.get("text") or "")
    code = payload.get("code")
    params = payload.get("params")
    if isinstance(code, str) and code:
        return present(code, params if isinstance(params, dict) else {}, fallback=text)
    return text


def _agent_depth(payload: dict[str, Any]) -> int:
    value = payload.get("agent_depth", 0)
    return value if isinstance(value, int) and value > 0 else 0
