"""Core memory reads and section updates."""

from __future__ import annotations

from ...core.logger import get_logger
from ...domain.model.memory import (
    ANCHOR_CURRENT,
    ANCHOR_NONE,
    ANCHOR_READ_ERROR,
    PROJECT_ID_RE,
    CoreMemory,
    CoreMemoryStoreError,
)
from ...domain.policy.memory import CoreMemoryPolicy, CoreMemoryUpdateError
from .dto import CoreMemoryUpdateDTO

logger = get_logger(__name__)

class CoreMemoryOps:
    def __init__(self, store, policy: CoreMemoryPolicy) -> None:
        self._core_memory_store = store
        self.core_memory_policy = policy

    def get_core_memory(self, project_id: str = "") -> CoreMemory | None:
        """Compose the current view. A project read failure keeps the global sections."""
        if self._core_memory_store is None:
            return None
        global_record = self._core_memory_store.load_global()
        bound = project_id.strip()
        anchor = ""
        state = ANCHOR_NONE
        if bound:
            try:
                project = self._core_memory_store.load_project(bound)
            except Exception as exc:
                self._diagnose(
                    "core_memory_read failure_type=%s scope=project project_id=%s",
                    type(exc).__name__,
                    bound,
                )
                state = ANCHOR_READ_ERROR
            else:
                anchor = project.project_anchor
                state = ANCHOR_CURRENT
        return CoreMemory(
            persona=global_record.persona,
            human_profile=global_record.human_profile,
            project_anchor=anchor,
            project_id=bound,
            project_anchor_state=state,
        )

    def update_core_memory(
        self,
        section: str,
        content: str = "",
        mode: str = "append",
        *,
        project_id: str = "",
    ) -> tuple[CoreMemoryUpdateDTO | None, str | None]:
        """Validate and persist one section, returning the text actually saved.

        ``human_profile`` is written to the global record. ``project_anchor``
        is written only to ``project_id``. The model cannot choose another
        project's file. An empty string is not a clear.
        """
        if self._core_memory_store is None:
            return None, "Core memory store is not configured"

        is_valid, err = self.core_memory_policy.validate_update(section, content, mode)
        if not is_valid:
            return None, err

        normalized_section = section.strip().lower()
        try:
            scope = self.core_memory_policy.scope_for(normalized_section)
        except CoreMemoryUpdateError as exc:
            return None, str(exc)

        if scope == "current_project":
            bound = project_id.strip()
            if not bound:
                return None, "缺少项目上下文，不能更新 project_anchor"
            if PROJECT_ID_RE.fullmatch(bound) is None:
                return None, "非法 project_id"
            try:
                saved = self._update_project_anchor(bound, content, mode)
            except (CoreMemoryUpdateError, CoreMemoryStoreError) as exc:
                return None, str(exc)
            return CoreMemoryUpdateDTO(
                section=normalized_section,
                content=saved,
                scope="current_project",
                project_id=bound,
            ), None

        try:
            saved = self._update_human_profile(content, mode)
        except CoreMemoryUpdateError as exc:
            return None, str(exc)
        return CoreMemoryUpdateDTO(
            section=normalized_section,
            content=saved,
            scope="global",
            project_id="",
        ), None

    def _update_human_profile(self, content: str, mode: str) -> str:
        def mutate(record) -> None:
            record.human_profile = self.core_memory_policy.compose_section(
                section="human_profile",
                current=record.human_profile,
                content=content,
                mode=mode,
            )

        return self._core_memory_store.update_global(mutate).human_profile  # type: ignore[union-attr]

    def _update_project_anchor(self, project_id: str, content: str, mode: str) -> str:
        def mutate(record) -> None:
            record.project_anchor = self.core_memory_policy.compose_section(
                section="project_anchor",
                current=record.project_anchor,
                content=content,
                mode=mode,
            )
        return self._core_memory_store.update_project(project_id, mutate).project_anchor  # type: ignore[union-attr]

    def _load_core(self, project_id: str = "") -> CoreMemory | None:
        try:
            return self.get_core_memory(project_id)
        except Exception as exc:
            self._diagnose("core_memory_read failure_type=%s scope=global", type(exc).__name__)
            return None

    def _diagnose(self, message: str, *args: object) -> None:
        try:
            logger.info(message, *args)
        except Exception:
            return
