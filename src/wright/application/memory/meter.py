"""Separate extraction, selection, and injection observations.

Token totals stay on the existing usage observer. These records describe the
same snapshots; they are not added again, and injection estimates are not
usage at all. Missing usage stays unknown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ...core.logger import get_logger
from ...domain.model.llm import UsageRecord

logger = get_logger(__name__)


@dataclass(frozen=True)
class ModelCallObservation:
    kind: str
    status: str
    reason_codes: tuple[str, ...] = ()
    attempts: int = 0
    duration_ms: float | None = None
    model: str = ""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    usage_available: bool = False
    written: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "attempts": self.attempts,
            "duration_ms": self.duration_ms,
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "usage_available": self.usage_available,
            "written": self.written,
        }


@dataclass(frozen=True)
class InjectionObservation:
    """What one main-model request actually received.

    ``selected`` is the selector choice. ``rendered`` survived the episode
    text budget. ``injected`` is what this request sent. A context drop leaves
    ``injected`` empty even when ``rendered`` is not.
    """

    selected_episode_ids: tuple[str, ...] = ()
    rendered_episode_ids: tuple[str, ...] = ()
    budget_skipped_ids: tuple[str, ...] = ()
    injected_episode_ids: tuple[str, ...] = ()
    semantic_estimated_tokens: int = 0
    episode_estimated_tokens: int = 0
    context_omitted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected_episode_ids": list(self.selected_episode_ids),
            "rendered_episode_ids": list(self.rendered_episode_ids),
            "budget_skipped_ids": list(self.budget_skipped_ids),
            "injected_episode_ids": list(self.injected_episode_ids),
            "semantic_estimated_tokens": self.semantic_estimated_tokens,
            "episode_estimated_tokens": self.episode_estimated_tokens,
            "context_omitted": self.context_omitted,
        }


def _usage_fields(usage: UsageRecord | None) -> dict[str, Any]:
    if usage is None:
        return {
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
            "usage_available": False,
        }
    return {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
        "usage_available": True,
    }


@dataclass
class MemoryMeter:
    """In-memory log. Recording never raises into the agent."""

    extractions: list[ModelCallObservation] = field(default_factory=list)
    selections: list[ModelCallObservation] = field(default_factory=list)
    injections: list[InjectionObservation] = field(default_factory=list)

    def record_model_call(
        self,
        kind: str,
        *,
        status: str,
        reason_codes: tuple[str, ...] = (),
        attempts: int = 0,
        duration_ms: float | None = None,
        model: str = "",
        usage: UsageRecord | None = None,
        written: int = 0,
    ) -> None:
        try:
            fields = _usage_fields(usage)
            event = ModelCallObservation(
                kind=kind,
                status=status,
                reason_codes=reason_codes,
                attempts=attempts,
                duration_ms=duration_ms,
                model=model,
                written=written,
                prompt_tokens=fields["prompt_tokens"],
                completion_tokens=fields["completion_tokens"],
                total_tokens=fields["total_tokens"],
                usage_available=fields["usage_available"],
            )
            if kind == "extraction":
                self.extractions.append(event)
            else:
                self.selections.append(event)
            logger.info(
                "memory_%s status=%s reasons=%s attempts=%s written=%s usage_available=%s",
                kind,
                status,
                ",".join(reason_codes),
                attempts,
                written,
                fields["usage_available"],
            )
        except Exception:
            logger.debug("memory meter failed", exc_info=True)

    def note_injection(
        self,
        *,
        selected_episode_ids: tuple[str, ...] = (),
        rendered_episode_ids: tuple[str, ...] = (),
        budget_skipped_ids: tuple[str, ...] = (),
        semantic_estimated_tokens: int = 0,
        episode_estimated_tokens: int = 0,
        omitted: bool = False,
    ) -> None:
        try:
            injected = () if omitted else rendered_episode_ids
            sent_episode_tokens = 0 if omitted else episode_estimated_tokens
            event = InjectionObservation(
                selected_episode_ids=selected_episode_ids,
                rendered_episode_ids=rendered_episode_ids,
                budget_skipped_ids=budget_skipped_ids,
                injected_episode_ids=injected,
                semantic_estimated_tokens=semantic_estimated_tokens,
                episode_estimated_tokens=sent_episode_tokens,
                context_omitted=omitted,
            )
            self.injections.append(event)
            logger.info(
                "memory_injection selected=%s rendered=%s injected=%s omitted=%s",
                list(selected_episode_ids),
                list(rendered_episode_ids),
                list(injected),
                omitted,
            )
        except Exception:
            logger.debug("memory meter failed", exc_info=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "extractions": [item.to_dict() for item in self.extractions],
            "selections": [item.to_dict() for item in self.selections],
            "injections": [item.to_dict() for item in self.injections],
        }


__all__ = [
    "InjectionObservation",
    "MemoryMeter",
    "ModelCallObservation",
]
