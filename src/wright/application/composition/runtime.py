"""Assemble the process-local Wright runtime used by the terminal REPL."""

from __future__ import annotations

import os
import queue
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from dotenv import dotenv_values, load_dotenv

from wright.application.permission.workflow import AuthorizationWorkflow

from ...core.logger import get_logger
from ...core.paths import (
    artifact_dir,
    attachment_dir,
    ensure_project_state,
    project_id,
    project_mcp_config_path,
    session_dir,
    skill_directories,
    task_db_path,
    trace_dir,
    user_mcp_config_path,
    wright_home,
)
from ...domain.model.session import Session
from ...domain.model.session.errors import CheckpointError
from ...domain.model.tool_definition import Tool
from ...domain.model.workspace.project import ProjectContext
from ...domain.policy import (
    PermissionRequest,
    PermissionResponse,
)
from ...domain.policy.verifier import Verifier
from ...domain.prompt import get_role_instruction
from ...infrastructure.llm.llm import LLMClient
from ...infrastructure.persistence.autonomy_store import AutonomyStore
from ...infrastructure.persistence.session.repository import FileSessionRepository
from ...infrastructure.storage.artifacts import ArtifactStore
from ...infrastructure.storage.attachments import (
    AttachmentStore,
    DraftAttachments,
)
from ...infrastructure.storage.skill_registry import SkillRegistry
from ...infrastructure.tools import tools as base_tools
from ...infrastructure.tools.human_input import ask_user_tool
from ...infrastructure.tools.knowledge import optional_knowledge_tools
from ...infrastructure.tools.loop_tools import manage_loop_tool
from ...infrastructure.tools.mcp_client import (
    McpManager,
    load_mcp_configs,
)
from ...infrastructure.tools.skill_tools import resident_skill_tools
from ..agent import (
    Agent,
    AgentBackgroundRuntime,
    assemble_agent_components,
    bind_root_checkpoint,
    build_agent_tools,
    ensure_system_prompt,
    prepare_model_tools,
)
from ..execution.directory import DirectoryExecutionCoordinator
from ..execution.services import RuntimeServices
from ..lifecycle import LifecycleConfigError
from ..permission.service import PermissionService
from ..session_runtime.events import SessionEvents, session_status
from ..session_runtime.interaction import RoutedPrompter, UserPrompter
from ..session_runtime.live_resources import RuntimeResources
from ..session_runtime.loops import SessionLoopRegistry
from ..session_runtime.publisher import EventPublisher
from ..tool_execution.approval import InteractiveApprovalHandler
from ..tool_execution.capabilities import assemble_tool_capabilities
from .capabilities import local_permission_service
from .host import ApplicationHost
from .lifecycle_loader import load_lifecycle_manager
from .memory_assembly import (
    assemble_memory_manager,
    memory_tools,
)

logger = get_logger(__name__)
# The only settings a project .env can set; everything else belongs in ~/.wright/.env.
_PROJECT_ENV_KEYS = frozenset({"OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_CONTEXT_LIMIT", "OPENAI_MEMORY_MODEL", "TAVILY_API_KEY"})


@dataclass
class WrightRuntime:
    agent: Agent
    session_state: Session
    event_renderer: SessionEvents
    publisher: EventPublisher
    project_context: ProjectContext
    services: RuntimeServices
    event_queue: queue.Queue[tuple[str, object]]
    agent_idle: threading.Event
    lifecycle: Any
    mcp_manager: McpManager
    checkpoint_store: FileSessionRepository
    autonomy_store: AutonomyStore
    permissions: PermissionService
    assembled_base_tools: list[Tool]
    llm: LLMClient
    resumed: bool
    cancellation_event: threading.Event
    attachment_store: AttachmentStore
    artifact_store: ArtifactStore
    draft_attachments: DraftAttachments
    runtime_resources: RuntimeResources
    sandbox_control: Any = None
    interaction_broker: Any = None
    # The ApplicationHost owns durable scheduling, SQLite and durable workers.
    # A session catalog may retain it after this Session closes.
    application_host: ApplicationHost | None = None
    directory_coordinator: DirectoryExecutionCoordinator | None = None
    owns_application_host: bool = True
    shutdown_lock: threading.Lock = field(default_factory=threading.Lock)
    shutdown_complete: threading.Event = field(default_factory=threading.Event)


@dataclass(frozen=True)
class RuntimeConfig:
    """Explicit runtime inputs shared by CLI, TUI and Web assembly."""

    workspace: Path | None = None
    resume: str | None = None
    continue_latest: bool = False
    no_session_persistence: bool = False
    hooks_config: Path | None = None
    model: str | None = None
    transport: str | None = None
    trust_project_mcp: bool = False
    with_rag: bool = False
    mode: str = "coding"
    execution_mode: str | None = None


def _make_interaction_handler(prompter: UserPrompter):
    """Adapt ask_user to the dedicated interaction protocol."""

    def handler(request: PermissionRequest) -> PermissionResponse:
        arguments = request.arguments
        answer = prompter.prompt_user(
            question=arguments["question"].strip(),
            context=arguments.get("context", "").strip(),
            options=tuple(arguments.get("options") or ()),
        )
        if answer is None:
            return PermissionResponse("deny")
        return PermissionResponse(
            "allow_once", updated_arguments={**arguments, "answer": answer}
        )

    return handler


class _OpenedRuntime:
    """Resources acquired while assembling one runtime.

    ``abort`` releases whatever has been recorded, in the same order as a
    failed build used to, and a second call does nothing. A host passed in
    by the caller is not closed; only a host this build created is.
    """

    def __init__(self, passed_host: ApplicationHost | None) -> None:
        self.passed_host = passed_host
        self.cancellation_event = threading.Event()
        self.session_state: Session | None = None
        self.runtime_resources: RuntimeResources | None = None
        self.interaction_broker: Any = None
        self.loop_registry: SessionLoopRegistry | None = None
        self.background_runtime: AgentBackgroundRuntime | None = None
        self.autonomy_store: AutonomyStore | None = None
        self.created_host: ApplicationHost | None = None
        self.mcp_manager: McpManager | None = None
        self.publisher: EventPublisher | None = None
        self.owns_publisher = False
        self._aborted = False

    def abort(self) -> None:
        if self._aborted:
            return
        self._aborted = True
        self.cancellation_event.set()
        session_state = self.session_state
        resources = self.runtime_resources
        if resources is not None and session_state is not None:
            cancelled_tasks = resources.close()
            session_state.mark_commands_cancel_requested(
                cancelled_tasks, "runtime assembly failed"
            )
        broker = self.interaction_broker
        if broker is not None:
            broker.close()
        try:
            if self.loop_registry is not None:
                self.loop_registry.close()
        finally:
            try:
                if self.created_host is not None:
                    self.created_host.close()
            finally:
                try:
                    if self.background_runtime is not None and session_state is not None:
                        self.background_runtime.shutdown(session_state.control_plane)
                finally:
                    try:
                        if self.passed_host is None and self.autonomy_store is not None:
                            self.autonomy_store.close()
                    finally:
                        if self.mcp_manager is not None:
                            self.mcp_manager.shutdown()
                        if self.owns_publisher and self.publisher is not None:
                            self.publisher.close()


def _open_session(
    *,
    config: RuntimeConfig,
    checkpoint_store: FileSessionRepository,
    workspace_dir: Path,
    project_root: Path,
    project_context: ProjectContext,
    session_id: str | None,
    resume_chooser: Callable[[list[dict]], str] | None,
) -> tuple[Session, bool]:
    """Create a session or restore one. Raises CheckpointError."""
    resume_arg = config.resume
    if resume_arg is not None:
        resume_id = resume_arg.strip()
        if not resume_id:
            recent = checkpoint_store.list_recent_sessions(limit=5)
            if resume_chooser is None:
                raise CheckpointError("Resuming a session requires an explicit session_id")
            resume_id = resume_chooser(recent)
        return checkpoint_store.load(resume_id), True
    if config.continue_latest:
        return checkpoint_store.load_latest(), True
    return Session.create(
        initial_goal="(interactive session)",
        workspace_dir=workspace_dir,
        session_id=session_id,
        project_root=project_root,
        environment=project_context.environment,
        execution_mode=config.execution_mode or "sandbox",
        base_commit=project_context.base_commit,
        branch_name=project_context.branch_name,
    ), False


def load_env() -> None:
    """Load trusted configuration, then only credentials from the current project.

    A project's .env can come from a cloned repository or a sandboxed command,
    so it must not reach permission, trust, path or endpoint settings.
    """
    load_dotenv(wright_home() / ".env")
    project_env = Path.cwd() / ".env"
    if not project_env.is_file():
        return
    values = dotenv_values(project_env)
    ignored = sorted(key for key in values if key not in _PROJECT_ENV_KEYS)
    if ignored:
        logger.warning(
            "Ignored settings in %s; move them to %s: %s",
            project_env, wright_home() / ".env", ", ".join(ignored),
        )
    # A key meant for a custom endpoint must not reach the default one instead.
    drop_key = "OPENAI_BASE_URL" in ignored and not os.getenv("OPENAI_BASE_URL")
    for key in _PROJECT_ENV_KEYS:
        value = values.get(key)
        if value is not None and key not in os.environ and not (drop_key and key == "OPENAI_API_KEY"):
            os.environ[key] = value


def _trusted_mcp_config_paths(
    workspace: Path, config: RuntimeConfig
) -> tuple[list[Path], Path | None]:
    """Return executable MCP configs and any ignored project config."""
    paths = [user_mcp_config_path()]
    project = project_mcp_config_path(workspace)
    trusted = config.trust_project_mcp or os.getenv(
        "WRIGHT_TRUST_PROJECT_MCP"
    ) == "1"
    if trusted:
        paths.append(project)
        return paths, None
    return paths, project if project.is_file() else None


def _restore_permissions(project_root: Path, session_state: Session) -> PermissionService:
    permissions = local_permission_service(project_root)
    try:
        permissions.restore(session_state, permissions.backend_factory(session_state.workspace_dir, session_state.get_cwd))
        if session_state.authorization.permission_mode is None:
            permissions.set_modes(session_state, permission_mode=permissions.snapshot(session_state).mode)
    except (OSError, ValueError, TypeError) as exc:
        raise SystemExit(f"Could not restore current permissions: {exc}") from exc
    return permissions


def _approval_workflow(interactive: bool, user_prompter: Any, events: SessionEvents) -> AuthorizationWorkflow:
    """Approval adapters only collect a structured choice; the resolver owns policy."""
    if not interactive:
        return AuthorizationWorkflow()
    return AuthorizationWorkflow(
        approval_handler=InteractiveApprovalHandler(user_prompter, notify_phase=events.on_tool_phase),
        interaction_handler=_make_interaction_handler(user_prompter),
    )


def _bind_interaction_persistence(
    interaction_target: Any,
    session_state: Session,
    autonomy_store: AutonomyStore,
    publisher_reader: Callable[[], EventPublisher],
) -> None:
    bind_persistence = getattr(interaction_target, "set_persistence", None)
    if callable(bind_persistence):
        interaction_scope = f"session:{session_state.session_id}"

        def record_interaction(request_id, kind, payload) -> None:
            active = session_state.active_run()
            autonomy_store.record_interaction(
                interaction_scope, request_id, kind=str(kind), payload=payload,
                run_id=active.run_id if active is not None else "",
            )
            publisher_reader().publish(
                "session.status_changed",
                session_status(
                    session_state.session_id, lifecycle="open", execution="waiting_for_input",
                    queue_reason="waiting for permission or a reply",
                ),
            )

        def resolve_interaction(request_id, _kind, resolution) -> None:
            status = str(resolution.get("status") or "resolved")
            if status not in {"approved", "denied", "cancelled", "resolved"}:
                status = "cancelled"
            autonomy_store.resolve_interaction(
                interaction_scope,
                request_id,
                resolution={
                    "status": status,
                    "choice": resolution.get("choice"),
                    "tool_name": resolution.get("tool_name", ""),
                },
                status=status,
            )
            publisher_reader().publish(
                "session.status_changed",
                session_status(
                    session_state.session_id, lifecycle="open", execution="running",
                    queue_reason="",
                ),
            )

        bind_persistence(record_interaction, resolve_interaction)


def _is_interactive(interaction_broker: Any, prompter: UserPrompter | None) -> bool:
    env_interactive = os.getenv("WRIGHT_PERMISSION_INTERACTIVE")
    tty_interactive = (
        env_interactive == "1" if env_interactive is not None else sys.stdin.isatty()
    )
    # Web and TUI pass a broker and collect elsewhere. A CLI prompter still
    # follows the tty, so a piped session does not wait on a hub nobody reads.
    return tty_interactive or (
        interaction_broker is not None and prompter is None
    )


def _select_base_tools(config: RuntimeConfig) -> tuple[list, list]:
    if config.with_rag:
        try:
            knowledge_tools = optional_knowledge_tools(enabled=True)
        except TypeError:
            knowledge_tools = optional_knowledge_tools()
    else:
        knowledge_tools = optional_knowledge_tools()
    active_base_tools = base_tools
    if config.mode == "general":
        coding_only = {"edit_file", "write_file", "execute_command"}
        active_base_tools = [t for t in base_tools if t.name not in coding_only]
    return active_base_tools, knowledge_tools


def _create_llm_clients(
    config: RuntimeConfig,
    session_state: Session,
    model: str,
    attachment_store: AttachmentStore,
    artifact_store: ArtifactStore,
    *,
    base_url: str | None,
    api_key: str | None,
    context_limit: int | None,
) -> tuple[LLMClient, LLMClient]:
    requested_transport = (
        session_state.llm_transport
        or config.transport
        or os.getenv("WRIGHT_LLM_TRANSPORT", "auto")
    )
    llm_client = LLMClient(
        base_url=base_url,
        api_key=api_key,
        model=model,
        context_limit=context_limit or 128000,
        transport=requested_transport,
        attachment_store=attachment_store,
        artifact_store=artifact_store,
    )
    session_state.llm_transport = llm_client.transport_name
    # 记忆的召回/提取 side-query 可选用更便宜的模型省钱(对标 memdir 用 Sonnet 选记忆)。
    # 配了 OPENAI_MEMORY_MODEL 就单独建个非流式 client,否则复用主 client。
    memory_model = os.getenv("OPENAI_MEMORY_MODEL")
    selector_llm = (
        LLMClient(
            base_url=base_url,
            api_key=api_key,
            model=memory_model,
            stream=False,
            transport=session_state.llm_transport,
        )
        if memory_model
        else llm_client
    )
    return llm_client, selector_llm


def _open_autonomy_store(
    project_root: Path,
    workspace_dir: Path,
    session_id: str,
    application_host: ApplicationHost | None,
) -> AutonomyStore:
    autonomy_store = AutonomyStore(
        task_db_path(project_root),
        session_id=session_id,
        workspace_dir=workspace_dir,
    )
    # A resumed Web session can rebind its pre-existing application owner.
    # Its durable store is the durable fact source; do not create a competing
    # scheduler/owner merely to reconstruct a conversation runtime.
    if application_host is not None:
        try:
            scheduler = application_host.scheduler_for(autonomy_store)
            autonomy_store = application_host.retained_store(scheduler.session_id)
        except Exception:
            autonomy_store.close()
            raise
    return autonomy_store


def assemble_runtime(
    config: RuntimeConfig,
    *,
    project_context: ProjectContext | None = None,
    publisher: EventPublisher | None = None,
    interaction_broker: Any = None,
    prompter: UserPrompter | None = None,
    session_id: str | None = None,
    start_automation: bool = True,
    automation_session_id: str | None = None,
    application_host: ApplicationHost | None = None,
    directory_coordinator: DirectoryExecutionCoordinator | None = None,
    resume_chooser: Callable[[list[dict]], str] | None = None,
) -> WrightRuntime:
    load_env()

    base_url = os.getenv("OPENAI_BASE_URL")
    api_key = os.getenv("OPENAI_API_KEY")
    requested_model = config.model
    configured_model = os.getenv("OPENAI_MODEL")
    context_limit_raw = os.getenv("OPENAI_CONTEXT_LIMIT")
    context_limit = int(context_limit_raw) if context_limit_raw else None

    requested_workspace = (config.workspace or Path.cwd()).expanduser().resolve()
    project_context = project_context or ProjectContext.local(requested_workspace)
    workspace_dir = project_context.execution_root
    project_root = project_context.project_root
    if not workspace_dir.is_dir():
        raise SystemExit(f"Workspace does not exist: {workspace_dir}")
    ensure_project_state(project_root)
    logger.info("workspace=%s", workspace_dir)
    logger.info("state=%s", session_dir(project_root).parent)

    # 多轮对话:session 整段存活,每轮把用户输入 append 进同一条历史；Agent.run 会把
    # user_goal 更新为当前任务，供 Verifier 和 checkpoint 使用。
    checkpoint_store = FileSessionRepository(session_dir(project_root))
    try:
        session_state, resumed = _open_session(
            config=config,
            checkpoint_store=checkpoint_store,
            workspace_dir=workspace_dir,
            project_root=project_root,
            project_context=project_context,
            session_id=session_id,
            resume_chooser=resume_chooser,
        )
    except CheckpointError as exc:
        raise SystemExit(f"Could not resume the session: {exc}") from exc

    permissions = _restore_permissions(project_root, session_state)

    # An explicit --model wins. Otherwise resuming retains the model selected
    # in that session, falling back to OPENAI_MODEL for new/legacy sessions.
    model = requested_model or session_state.model_name or configured_model
    if not model:
        raise ValueError("OPENAI_MODEL or --model is required")
    session_state.model_name = model
    attachment_store = AttachmentStore(attachment_dir(project_root), session_state.session_id)
    artifact_store = ArtifactStore(artifact_dir(project_root))
    llm_client, selector_llm = _create_llm_clients(
        config, session_state, model, attachment_store, artifact_store,
        base_url=base_url, api_key=api_key, context_limit=context_limit,
    )
    llm_client.session_attachments = session_state.attachments
    draft_attachments = DraftAttachments(attachment_store, session_state.attachments)
    runtime_resources = None  # created after the event queue exists

    opened = _OpenedRuntime(application_host)
    opened.session_state = session_state
    opened.interaction_broker = interaction_broker
    opened.owns_publisher = publisher is None
    event_queue: queue.Queue[tuple[str, object]] = queue.Queue()
    cancellation_event = opened.cancellation_event
    # agent_idle：loop 调度与主输入框都等它；忙碌时不画「你的指令」。
    agent_idle = threading.Event()
    agent_idle.set()
    background_runtime = AgentBackgroundRuntime(event_queue)
    autonomy_store = _open_autonomy_store(
        project_root,
        workspace_dir,
        automation_session_id or session_state.session_id,
        application_host,
    )
    opened.background_runtime = background_runtime
    opened.autonomy_store = autonomy_store
    from ..command.execution import CommandExecution
    from ..execution.identity import bind_identity

    identity = bind_identity(session_state, autonomy_store)
    command_execution = CommandExecution(
        session_state,
        identity,
        notify=lambda command_id: event_queue.put(("TASK_DONE", command_id)),
    )
    runtime_resources = RuntimeResources(
        session_state.session_id,
        commands=command_execution,
        identity=identity,
    )
    opened.runtime_resources = runtime_resources
    loop_registry = SessionLoopRegistry(event_queue, agent_idle)
    opened.loop_registry = loop_registry
    services = RuntimeServices(
        agent_background=background_runtime,
        durable_store=autonomy_store,
        loop_registry=loop_registry,
    )
    interaction_target = interaction_broker
    if interaction_target is not None:
        # The publisher may be created later in assembly, so read it when a
        # callback fires instead of binding the current value.
        _bind_interaction_persistence(
            interaction_target, session_state, autonomy_store, lambda: publisher
        )
    mcp_manager: McpManager | None = None
    constructed_application_host: ApplicationHost | None = application_host

    def abort_assembly() -> None:
        """Release resources constructed before a runtime became publishable."""
        opened.mcp_manager = mcp_manager
        opened.publisher = publisher
        opened.abort()
    try:
        lifecycle = load_lifecycle_manager(
            workspace_dir,
            session_state.session_id,
            config_path=(
                config.hooks_config
                if config.hooks_config is not None
                else None
            ),
            trace_dir=trace_dir(project_root),
        )
    except LifecycleConfigError as exc:
        abort_assembly()
        raise SystemExit(f"Could not load lifecycle hooks: {exc}") from exc
    lifecycle.emit(
        "session_start",
        {"resumed": resumed, "workspace_dir": str(workspace_dir)},
        root_turn_id=session_state.agent_root_turn_id,
    )

    # User MCP config is an explicit local preference. Project config is code
    # from the workspace and may start arbitrary stdio processes, so it is
    # opt-in instead of being trusted merely because the repository was opened.
    mcp_paths, ignored_project_mcp = _trusted_mcp_config_paths(workspace_dir, config)
    if ignored_project_mcp is not None:
        logger.warning(
            "Ignored untrusted project MCP config %s; pass --trust-project-mcp to load it",
            ignored_project_mcp,
        )
    try:
        mcp_configs = load_mcp_configs(mcp_paths)
        mcp_manager = McpManager(mcp_configs, artifact_store=artifact_store)
        opened.mcp_manager = mcp_manager
        mcp_tools = mcp_manager.start()
    except Exception:
        abort_assembly()
        raise

    publisher = publisher or EventPublisher(
        project_id=project_id(project_root),
        session_id=session_state.session_id,
    )
    opened.publisher = publisher
    if interaction_target is not None:
        from ..planning.projection import bind_plan_events

        bind_plan_events(publisher, session_state, interaction_target)
    event_renderer = SessionEvents(
        publisher,
        runtime_resources=runtime_resources,
    )
    user_prompter = RoutedPrompter(interaction_target, fallback=prompter)
    coordinator = directory_coordinator or DirectoryExecutionCoordinator()

    from ..permission.checkpoint import SessionCheckpoint
    if not config.no_session_persistence:
        permissions.bind_checkpoint(session_state.session_id, SessionCheckpoint(checkpoint_store, session_state))
    permissions.policy_updated = lambda owner_id, payload: publisher.publish("session.policy_updated", payload)

    permission_workflow = _approval_workflow(
        _is_interactive(interaction_broker, prompter), user_prompter, event_renderer
    )

    # 给主 Agent 装上"基础工具 + spawn_agent"的分层工具集:depth=0 是主 Agent,
    # 默认只委派叶子子 Agent（max_depth=1）。嵌套 spawn 仍由控制面支持，
    # 无人值守 durable run 会显式打开第二层。
    # knowledge_search 是只读检索：启用后放进 base，让子 Agent 也能查知识库。
    active_base_tools, knowledge_tools = _select_base_tools(config)

    # defer_to_model 只推迟 MCP 工具的 schema 暴露。McpManager.start() 已经
    # 在启动时连接服务器并发现工具，这里不是延迟初始化。
    assembled_base = [
        *active_base_tools,
        *(replace(tool, defer_to_model=True) for tool in mcp_tools),
        *knowledge_tools,
    ]
    # ask_user、manage_loop、记忆与 load_skill 只给主 Agent:都在 build_agent_tools 之后
    # 【单独追加】，不进 base_tools。子 Agent 不能绕过父 Agent 直接打断人；
    # 它若信息不足，应把缺口作为结果交回父 Agent。子 Agent 也保持无长期记忆、
    # 无 skill 加载器的纯净上下文——委派时把需要的流程写进任务描述。
    # loop 同理：会话内重跑必须看见当前对话，不能下放到隔离的子 Agent。
    memory_manager = assemble_memory_manager(
        llm_client,
        selector_llm=selector_llm,
        session_repository=checkpoint_store,
    )
    skill_registry = SkillRegistry(skill_directories(workspace_dir))
    # 注册与 schema 暴露分开：加载器常驻注册，有技能时才出现在当轮 schema 和目录里。
    skill_tools = resident_skill_tools(skill_registry)
    tools = [
        *build_agent_tools(
            llm_client,
            assembled_base,
            depth=0,
            max_depth=1,
            permission_workflow=permission_workflow,
            enable_scheduling=True,
            publisher=publisher,
            permission_service=permissions,
        ),
        ask_user_tool,
        manage_loop_tool,
        *memory_tools(memory_manager),
        *skill_tools,
    ]

    # Automation is process/application owned, not consumed by this Session's
    # event worker.  The foreground Session keeps its own subagent runtime but
    # all scheduling tools resolve this host scheduler through RuntimeServices.
    # Assign it before tool-capability assembly so the initial view includes it.
    if constructed_application_host is None:
        constructed_application_host = ApplicationHost(
            workspace_dir=workspace_dir,
            store=autonomy_store,
            llm=llm_client,
            # Host builds its own MCP wrappers and never retains tools bound
            # to the Session-owned manager above.
            base_tools=[*active_base_tools, *knowledge_tools],
            permission_service=permissions,
            mcp_configs=mcp_configs,
            artifact_store=artifact_store,
            directory_coordinator=coordinator,
        )
        opened.created_host = constructed_application_host
    else:
        constructed_application_host.bind_coordinator(coordinator)
    command_execution.attach_directory(coordinator)
    services.job_scheduler = constructed_application_host.scheduler_for(autonomy_store)
    assembly = assemble_tool_capabilities(
        session_state,
        services,
        runtime_resources,
        workspace_dir=workspace_dir,
        expose_scheduling=True,
        permissions=permissions,
    )

    prepared = prepare_model_tools(session_state, tools)
    role_instruction = get_role_instruction(config.mode)
    ensure_system_prompt(session_state, prepared, memory_manager, role_instruction=role_instruction)
    components = assemble_agent_components(
        session_state=session_state,
        events=event_renderer,
        prepared=prepared,
        assembly=assembly,
        keep_recent_tool_results=3,
        permission_workflow=permission_workflow,
        cancellation_check=cancellation_event.is_set,
        allow_background_tasks=True,
        on_shell_task_done=lambda task_id: event_queue.put(("TASK_DONE", task_id)),
        lifecycle=lifecycle,
        execution_journal=None,
    )
    agent = Agent(
        llm_client,
        session_state,
        event_renderer,
        prepared,
        assembly,
        components=components,
        memory=memory_manager,
        verifier=Verifier(),
        checkpoint_store=(
            None
            if config.no_session_persistence
            else checkpoint_store
        ),
        lifecycle=lifecycle,
        skills=skill_registry if skill_tools else None,
        services=services,
        runtime_resources=runtime_resources,
        expose_scheduling=True,
    )
    bind_root_checkpoint(agent)

    try:
        if start_automation:
            constructed_application_host.start()
        loop_registry.start()
    except Exception:
        abort_assembly()
        raise

    from ...infrastructure.runtime.sandbox import LocalSandboxControl

    return WrightRuntime(
        agent=agent,
        session_state=session_state,
        event_renderer=event_renderer,
        publisher=publisher,
        project_context=project_context,
        services=services,
        event_queue=event_queue,
        agent_idle=agent_idle,
        lifecycle=lifecycle,
        mcp_manager=mcp_manager,
        checkpoint_store=checkpoint_store,
        autonomy_store=autonomy_store,
        permissions=agent.executor.permissions,
        assembled_base_tools=assembled_base,
        llm=llm_client,
        resumed=resumed,
        cancellation_event=cancellation_event,
        attachment_store=attachment_store,
        artifact_store=artifact_store,
        draft_attachments=draft_attachments,
        runtime_resources=runtime_resources,
        interaction_broker=interaction_broker,
        application_host=constructed_application_host,
        directory_coordinator=coordinator,
        sandbox_control=LocalSandboxControl(),
    )


def shutdown_runtime(rt: WrightRuntime) -> None:
    # SessionRunner invokes us when its worker actually exits; direct legacy
    # callers are harmless too.  Do not double-close MCP/database resources.
    with rt.shutdown_lock:
        if rt.shutdown_complete.is_set():
            return
        rt.shutdown_complete.set()
    rt.cancellation_event.set()
    if rt.interaction_broker is not None:
        rt.interaction_broker.close()
    cancelled_tasks = rt.runtime_resources.close()
    rt.session_state.mark_commands_cancel_requested(
        cancelled_tasks, "runtime shutdown"
    )
    # A selected-but-never-sent image has no conversational meaning and should
    # not become durable just because the host exits normally.  Referenced
    # images deliberately remain available for checkpoint resume.
    referenced = {
        attachment_id
        for record in rt.session_state.message_records
        for attachment_id in record.message.get("attachments", [])
        if isinstance(attachment_id, str)
    }
    for attachment_id, record in list(rt.session_state.attachments.items()):
        if attachment_id not in referenced:
            rt.attachment_store.remove(record)
            del rt.session_state.attachments[attachment_id]
    if rt.agent.checkpoint_store is not None:
        try:
            rt.agent.checkpoint_store.save(rt.session_state)
        except Exception as exc:
            rt.event_renderer.on_checkpoint_error(str(exc))
    if rt.services.loop_registry is not None:
        rt.services.loop_registry.close()
    if rt.owns_application_host and rt.application_host is not None:
        rt.application_host.close()
    if rt.services.agent_background is not None:
        rt.services.agent_background.shutdown(rt.session_state.control_plane)
    # The durable store is owned by ApplicationHost.  A Web RuntimeManager can
    # deliberately retain that owner after this individual Session closes.
    if rt.application_host is None and rt.services.durable_store is not None:
        rt.services.durable_store.close()
    # 关闭 MCP session / stdio 子进程,避免残留进程。
    rt.mcp_manager.shutdown()
    rt.publisher.close()
