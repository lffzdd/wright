"""Application-side adapters for constructing a tool runtime."""

from __future__ import annotations

from pathlib import Path

from ..core.processes import RuntimeResources
from ..domain.policy.scope import AccessScope
from ..domain.policy.types import GrantTarget, InvocationGrant, InvocationIdentity
from ..infrastructure.runtime import AuthorizedExecution
from ..infrastructure.tools.runtime import ToolRuntime
from .tool_capabilities import assemble_tool_capabilities


def tool_runtime_for_session(
    session,
    *,
    services=None,
    runtime_resources: RuntimeResources | None = None,
    workspace_dir=None,
    cwd_provider=None,
    execution_backend=None,
    **kwargs,
) -> ToolRuntime:
    """Build a bounded runtime for direct tool adapters and tests."""

    assembly = assemble_tool_capabilities(
        session,
        services,
        runtime_resources,
        workspace_dir=workspace_dir,
        cwd_provider=cwd_provider,
        execution_backend=execution_backend,
    )
    capabilities = assembly.capabilities
    resources = assembly.runtime_resources
    backend = assembly.backend
    workspace = (
        workspace_dir or getattr(session, "workspace_dir", None) or Path.cwd()
    ).resolve()
    additional = ()
    if session is not None:
        snapshot = getattr(session, "working_directories_snapshot", None)
        additional = (
            tuple(snapshot())
            if callable(snapshot)
            else tuple(getattr(session, "additional_working_directories", ()) or ())
        )
    access_scope = AccessScope(workspace, additional)
    execution = None
    if backend is not None:
        roots = access_scope.roots
        targets = tuple(
            target
            for root in roots
            for target in (
                GrantTarget(backend.resolve_path(str(root)), "file_read", True),
                GrantTarget(backend.resolve_path(str(root)), "file_write", True),
            )
        )
        cwd = backend.cwd()
        execution = AuthorizedExecution(
            backend,
            InvocationGrant(
                InvocationIdentity(capabilities.scope.session_id, call_id="adapter"),
                cwd.environment_id,
                cwd,
                frozenset({"file_read", "file_write", "shell"}),
                targets,
                command=None,
            ),
            dynamic_cwd=True,
        )
    return ToolRuntime(
        capabilities=capabilities,
        execution=execution,
        access_scope=access_scope,
        runtime_resources=resources,
        **kwargs,
    )
