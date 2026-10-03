"""Application-side adapters for constructing a tool runtime."""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath

from ...domain.policy.permission.scope import AccessScope, is_under
from ...domain.policy.permission.types import (
    GrantTarget,
    InvocationGrant,
    InvocationIdentity,
)
from ...infrastructure.runtime import AuthorizedExecution
from ...infrastructure.tools.runtime import ToolRuntime
from ..command.execution import CommandExecution
from ..execution.identity import bind_identity
from ..session.live_resources import RuntimeResources
from .capabilities import assemble_tool_capabilities


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

    if session is not None:
        store = getattr(services, "durable_store", None)
        if runtime_resources is None:
            runtime_resources = RuntimeResources(session.session_id)
        if runtime_resources.identity is None:
            runtime_resources.identity = bind_identity(session, store)
        if runtime_resources.commands is None:
            runtime_resources.commands = CommandExecution(
                session,
                runtime_resources.identity,
                allow_background=kwargs.get("allow_background_tasks", True),
                notify=kwargs.get("notify_background_done"),
            )
    assembly = assemble_tool_capabilities(
        session,
        services,
        runtime_resources,
        workspace_dir=workspace_dir,
        cwd_provider=cwd_provider,
        execution_backend=execution_backend,
        expose_scheduling=getattr(services, "durable_store", None) is not None,
    )
    capabilities = assembly.capabilities
    backend = assembly.backend
    workspace = (
        workspace_dir or getattr(session, "workspace_dir", None) or Path.cwd()
    ).resolve()
    if backend is not None and backend.environment_id != "local":
        canonical = backend.resolve_path(".").value
        workspace = PureWindowsPath(canonical) if PureWindowsPath(canonical).is_absolute() else PurePosixPath(canonical)
        access_scope = AccessScope(workspace)
    elif session is not None:
        access_scope = assembly.permissions.snapshot(session).scope
    else:
        access_scope = AccessScope(workspace)
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
            if target.operation != "file_write" or not any(root == readonly or is_under(root, readonly) for readonly in access_scope.read_only)
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
                blocked_paths=tuple(backend.resolve_path(str(path)) for path in access_scope.protected),
                shell_readable=tuple(backend.resolve_path(str(root)) for root in access_scope.roots),
                shell_writable=tuple(target.path for target in targets if target.operation == "file_write"),
                shell_readonly=tuple(backend.resolve_path(str(root)) for root in access_scope.read_only),
            ),
            dynamic_cwd=True,
        )
    return ToolRuntime(
        capabilities=capabilities,
        execution=execution,
        access_scope=access_scope,
        **kwargs,
    )
