"""Build one standalone Agent over the local runtime; hosts assemble their own."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from ...domain.gateway.llm_gateway import ILLMProvider
from ...domain.gateway.skills import ISkillCatalog
from ...domain.model.agent import AgentProfile
from ...domain.model.llm import UsageRecord
from ...domain.model.session import Session
from ...domain.model.tool_definition import Tool
from ...domain.policy.verifier import Verifier
from ..agent.assembly import (
    assemble_agent_components,
    bind_root_checkpoint,
    ensure_system_prompt,
    prepare_model_tools,
)
from ..agent.runner import Agent
from ..execution.services import RuntimeServices
from ..memory import MemoryManager
from ..permission.workflow import AuthorizationWorkflow
from ..session_runtime.events import SessionEvents
from ..session_runtime.live_resources import RuntimeResources
from ..session_runtime.publisher import open_session_events
from ..tool_execution.capabilities import CapabilityAssembly
from .capabilities import assemble_tool_capabilities

if TYPE_CHECKING:
    from ...domain.gateway.session_repository import SessionRepository


def create_agent(
    llm: ILLMProvider,
    tools: list[Tool],
    session_state: Session,
    events: SessionEvents | None = None,
    tool_timeout: float = 30,
    context_watermark: float = 0.75,
    keep_recent_tool_results: int = 3,
    max_consecutive_invalid: int = 3,
    permission_workflow: AuthorizationWorkflow | None = None,
    cancellation_check: Callable[[], bool] | None = None,
    memory: MemoryManager | None = None,
    verifier: Verifier | None = None,
    max_verification_retries: int = 3,
    checkpoint_store: SessionRepository | None = None,
    usage_observer: Callable[[UsageRecord], None] | None = None,
    allow_background_tasks: bool = True,
    on_shell_task_done: Callable[[str], None] | None = None,
    lifecycle=None,
    skills: ISkillCatalog | None = None,
    services: RuntimeServices | None = None,
    runtime_resources: RuntimeResources | None = None,
    profile: AgentProfile | None = None,
    execution_journal=None,
    execution_journal_factory=None,
    on_run_started: Callable[[str], None] | None = None,
    assembly: CapabilityAssembly | None = None,
    role_instruction: str = "",
    expose_scheduling: bool = False,
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
            expose_scheduling=expose_scheduling,
        )
        runtime_resources = assembly.runtime_resources or resources
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
        permission_workflow=permission_workflow,
        cancellation_check=cancellation_check,
        allow_background_tasks=allow_background_tasks,
        on_shell_task_done=on_shell_task_done,
        lifecycle=lifecycle,
        execution_journal=execution_journal,
    )
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
        expose_scheduling=expose_scheduling,
    )
    bind_root_checkpoint(agent)
    return agent
