"""Assemble one Agent from already chosen tools, events, and capabilities.

This module wires collaborators. It is not the Agent run loop and does not
attach a renderer. Hosts that want a display subscribe one themselves.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from ...domain.model.agent import AgentProfile, CapabilityCatalog, CapabilitySnapshot
from ...domain.model.llm import UsageRecord
from ...domain.model.session import Session
from ...domain.policy import AuthorizationChange, PermissionResolver
from ...domain.policy.verifier import Verifier
from ...domain.prompt import build_system_prompt
from ...domain.protocol import encode_tools
from ...infrastructure.llm.llm import LLMClient
from ...infrastructure.tools.base import Tool
from ...infrastructure.tools.tool_search import (
    MAX_ACTIVE_DEFERRED_TOOLS,
    make_tool_search_tool,
)
from ..composition.services import RuntimeServices
from ..memory import MemoryManager
from ..session.events import SessionEvents
from ..session.live_resources import RuntimeResources
from ..session.publisher import open_session_events
from ..skills import SkillRegistry
from ..tool_execution.capabilities import (
    CapabilityAssembly,
    assemble_tool_capabilities,
)
from ..tool_execution.dispatch import ToolDispatchService
from .cancellation import CancellationToken
from .components import AgentComponents, PreparedTools
from .context import ContextBuilder, ContextCompactor

if TYPE_CHECKING:
    from ...infrastructure.persistence.session.repository import FileSessionRepository
    from .runner import Agent


def assemble_agent_components(
    *,
    session_state: Session,
    events: SessionEvents,
    prepared: PreparedTools,
    assembly: CapabilityAssembly,
    tool_timeout: float = 30,
    context_watermark: float = 0.75,
    keep_recent_tool_results: int = 3,
    permission_resolver: PermissionResolver | None = None,
    cancellation_check: Callable[[], bool] | None = None,
    allow_background_tasks: bool = True,
    on_shell_task_done: Callable[[str], None] | None = None,
    lifecycle=None,
    execution_journal=None,
    authorization_commit: Callable[[AuthorizationChange], None] | None = None,
) -> AgentComponents:
    """Build the collaborators bound to one Agent and one execution scope."""
    cancellation = CancellationToken(cancellation_check)
    compactor = ContextCompactor(
        on_compact=events.on_context_compact,
        context_watermark=context_watermark,
        keep_recent_tool_results=keep_recent_tool_results,
    )
    context_builder = ContextBuilder(compactor)
    tool_dispatcher = ToolDispatchService(
        {tool.name: tool for tool in prepared.tools},
        assembly,
        tool_timeout=tool_timeout,
        on_command_output=events.on_command_output,
        on_tool_output=events.on_tool_output,
        on_progress=events.on_agent_event,
        on_shell_task_done=on_shell_task_done,
        permission_resolver=permission_resolver,
        session=session_state,
        cancellation_check=cancellation.is_cancelled,
        allow_background_tasks=(
            allow_background_tasks and prepared.profile.allow_background_tasks
        ),
        lifecycle=lifecycle,
        capability_snapshot=prepared.capabilities,
        execution_journal=execution_journal,
        authorization_commit=authorization_commit,
    )
    return AgentComponents(cancellation, context_builder, tool_dispatcher)


def prepare_model_tools(
    session: Session,
    tools: list[Tool],
    profile: AgentProfile | None = None,
) -> PreparedTools:
    """Filter tools for this profile and attach the deferred-tool catalog."""
    catalog = CapabilityCatalog(tools)
    resolved = profile or AgentProfile(
        "root",
        catalog.names,
        allow_interaction=True,
        allow_background_tasks=True,
        allow_delegation=True,
    )
    capabilities = catalog.snapshot(resolved)
    runtime_tools = [
        tool for tool in capabilities.tools
        if (resolved.allow_interaction or not tool.requires_user_interaction)
        and (resolved.allow_delegation or tool.name not in {"spawn_agent", "get_agent_tree"})
    ]
    deferred_names = {
        tool.name
        for tool in runtime_tools
        if tool.expose_to_model and tool.defer_to_model
    }
    restored_active = [
        name
        for name in session.active_deferred_tools
        if name in deferred_names
    ][-MAX_ACTIVE_DEFERRED_TOOLS:]
    session.active_deferred_tools[:] = restored_active
    if any(tool.expose_to_model and tool.defer_to_model for tool in runtime_tools):
        runtime_tools.append(make_tool_search_tool(runtime_tools, session.active_deferred_tools))
    capabilities = CapabilitySnapshot(resolved, tuple(runtime_tools))
    schemas, names = encode_tools(
        runtime_tools, active_deferred=set(session.active_deferred_tools)
    )
    return PreparedTools(resolved, capabilities, runtime_tools, schemas, names)


def ensure_system_prompt(
    session: Session,
    prepared: PreparedTools,
    memory: MemoryManager | None,
    role_instruction: str = "",
) -> None:
    """Persist static instructions once; mutable memory is projected per request."""
    if session.message_records:
        return
    memory_section = memory.instructions() if memory else ""
    prompt_tools = [tool for tool in prepared.tools if tool.name != "tool_search"]
    effective_role = role_instruction or getattr(prepared.profile, "role_instruction", "")
    session.append_message({
        "role": "system",
        "content": build_system_prompt(
            prompt_tools,
            memory_section=memory_section,
            role_instruction=effective_role,
        ),
    })


def bind_root_checkpoint(agent: Agent) -> None:
    """Persist the root control plane. Child tasks checkpoint through their parent."""
    agent.attach_root_checkpoint()


def create_agent(
    llm: LLMClient,
    tools: list[Tool],
    session_state: Session,
    events: SessionEvents | None = None,
    tool_timeout: float = 30,
    context_watermark: float = 0.75,
    keep_recent_tool_results: int = 3,
    max_consecutive_invalid: int = 3,
    permission_resolver: PermissionResolver | None = None,
    cancellation_check: Callable[[], bool] | None = None,
    memory: MemoryManager | None = None,
    verifier: Verifier | None = None,
    max_verification_retries: int = 3,
    checkpoint_store: FileSessionRepository | None = None,
    usage_observer: Callable[[UsageRecord], None] | None = None,
    allow_background_tasks: bool = True,
    on_shell_task_done: Callable[[str], None] | None = None,
    lifecycle=None,
    skills: SkillRegistry | None = None,
    services: RuntimeServices | None = None,
    runtime_resources: RuntimeResources | None = None,
    profile: AgentProfile | None = None,
    execution_journal=None,
    execution_journal_factory=None,
    on_run_started: Callable[[str], None] | None = None,
    authorization_commit: Callable[[AuthorizationChange], None] | None = None,
    authorization_commit_factory=None,
    assembly: CapabilityAssembly | None = None,
    role_instruction: str = "",
    expose_autonomy: bool = False,
) -> Agent:
    """Prepare tools, the system prompt, and events, then build an Agent.

    Tests and scripts use this. Production hosts call the same steps themselves.
    """
    if events is None:
        events = open_session_events(session_state)
    if assembly is None:
        resources = runtime_resources or RuntimeResources(session_state.session_id)
        assembly = assemble_tool_capabilities(
            session_state,
            services,
            resources,
            execution_journal_factory=execution_journal_factory,
            authorization_commit_factory=authorization_commit_factory,
            expose_autonomy=expose_autonomy,
        )
        runtime_resources = assembly.runtime_resources or resources
    if authorization_commit is None and authorization_commit_factory is not None:
        authorization_commit = authorization_commit_factory(session_state)
    prepared = prepare_model_tools(session_state, tools, profile)
    ensure_system_prompt(
        session_state,
        prepared,
        memory,
        role_instruction=role_instruction or (profile.role_instruction if profile else ""),
    )
    components = assemble_agent_components(
        session_state=session_state,
        events=events,
        prepared=prepared,
        assembly=assembly,
        tool_timeout=tool_timeout,
        context_watermark=context_watermark,
        keep_recent_tool_results=keep_recent_tool_results,
        permission_resolver=permission_resolver,
        cancellation_check=cancellation_check,
        allow_background_tasks=allow_background_tasks,
        on_shell_task_done=on_shell_task_done,
        lifecycle=lifecycle,
        execution_journal=execution_journal,
        authorization_commit=authorization_commit,
    )
    from .runner import Agent

    agent = Agent(
        llm,
        session_state,
        events,
        prepared,
        assembly,
        components=components,
        max_consecutive_invalid=max_consecutive_invalid,
        memory=memory,
        verifier=verifier,
        max_verification_retries=max_verification_retries,
        checkpoint_store=checkpoint_store,
        usage_observer=usage_observer,
        lifecycle=lifecycle,
        skills=skills,
        services=services,
        runtime_resources=runtime_resources,
        execution_journal=execution_journal,
        execution_journal_factory=execution_journal_factory,
        on_run_started=on_run_started,
        authorization_commit=authorization_commit,
        authorization_commit_factory=authorization_commit_factory,
        expose_autonomy=expose_autonomy,
    )
    bind_root_checkpoint(agent)
    return agent


