"""The only policy for where a new session runs and which checkpoint resumes.

Adapters pass an intent through. They do not guess a default environment or
turn an empty resume id into "start fresh".
"""

from __future__ import annotations

from ...infrastructure.persistence.session.errors import CheckpointError

DEFAULT_ENVIRONMENT = "local"


class OpenIntentError(ValueError):
    def __init__(self, message: str, *, kind: str = "error") -> None:
        super().__init__(message)
        self.kind = kind


def resolve_new_environment(requested: str | None, *, git: bool) -> str:
    """New sessions run on the local checkout unless isolation is explicit."""

    if requested is None or requested == "":
        return DEFAULT_ENVIRONMENT
    if requested not in {"local", "worktree"}:
        raise OpenIntentError("environment must be local or worktree")
    if requested == "worktree" and not git:
        raise OpenIntentError(
            "worktree requires a git repository",
            kind="environment",
        )
    return requested


def load_resume_checkpoint(
    checkpoints: object,
    *,
    resume: str | None,
    continue_latest: bool,
    resume_chooser,
):
    """Resolve continue / blank resume / explicit id without dropping the intent.

    ``resume is None`` means the caller is not resuming. ``resume == ""`` means
    the caller asked to choose a saved session. A checkpoint is returned with
    its original environment still on the object; this function never rewrites it.
    """

    if continue_latest and resume not in (None, ""):
        raise OpenIntentError("continue and resume cannot be combined")
    if continue_latest:
        try:
            return checkpoints.load_latest()
        except CheckpointError as exc:
            raise OpenIntentError(str(exc), kind="not_found") from exc
    if resume is None:
        return None
    resume_id = resume.strip()
    if not resume_id:
        recent = list(checkpoints.list_recent_sessions(limit=12))
        if resume_chooser is None:
            raise OpenIntentError(
                "resuming a session requires an explicit session id",
                kind="not_found",
            )
        if not recent:
            raise OpenIntentError("no saved session to resume", kind="not_found")
        resume_id = str(resume_chooser(recent) or "").strip()
        if not resume_id:
            raise OpenIntentError("no session was selected", kind="not_found")
    try:
        return checkpoints.load(resume_id)
    except CheckpointError as exc:
        raise OpenIntentError(str(exc), kind="not_found") from exc


__all__ = [
    "DEFAULT_ENVIRONMENT",
    "OpenIntentError",
    "load_resume_checkpoint",
    "resolve_new_environment",
]
