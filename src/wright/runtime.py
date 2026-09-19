"""Assemble the process-local Wright runtime used by the terminal REPL."""

from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from prompt_toolkit import prompt

from .agent import Agent
from .agent_background import AgentBackgroundRuntime
from .application_host import ApplicationHost
from .artifacts import ArtifactStore
from .attachments import AttachmentStore, DraftAttachments
from .autonomy import AutonomyStore
from .checkpoint import CheckpointError, SessionCheckpointStore
from .interaction import InteractionHub
from .knowledge import optional_knowledge_tools
from .lifecycle import LifecycleConfigError, load_lifecycle_manager
from .llm import LLMClient
from .logger import get_logger
from .looping import SessionLoopRegistry
from .memory import MemoryManager
from .paths import (
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
)
from .permission import (
    FallbackApprovalHandler,
    InteractiveApprovalHandler,
    PermissionCheckResult,
    PermissionRequest,
    PermissionResolver,
    PermissionSettings,
    RuleBasedApprovalHandler,
    append_allow_rule,
    load_permission_settings,
)
from .processes import RuntimeResources
from .project import ProjectContext
from .renderer import ConsoleRenderer, Renderer
from .services import RuntimeServices
from .session import Session
from .skills import SkillRegistry, optional_skill_tools
from .subagent import build_agent_tools
from .tools import tools as base_tools
from .tools.ask_user_tool import ask_user_tool
from .tools.base import Tool
from .tools.loop_tools import manage_loop_tool
from .tools.mcp_client import McpManager, load_mcp_configs
from .ui_events import EventPublisher, PublishingRenderer
from .verifier import Verifier

logger = get_logger(__name__)


@dataclass
class WrightRuntime:
    agent: Agent
    session_state: Session
    renderer: Renderer
    event_renderer: PublishingRenderer
    publisher: EventPublisher
    project_context: ProjectContext
    services: RuntimeServices
    event_queue: queue.Queue[tuple[str, object]]
    agent_idle: threading.Event
    lifecycle: Any
    mcp_manager: McpManager
    checkpoint_store: SessionCheckpointStore
    autonomy_store: AutonomyStore
    permission_settings: PermissionSettings
    assembled_base_tools: list[Tool]
    llm: LLMClient
    resumed: bool
    cancellation_event: threading.Event
    attachment_store: AttachmentStore
    artifact_store: ArtifactStore
    draft_attachments: DraftAttachments
    runtime_resources: RuntimeResources
    interaction_broker: Any = None
    # The ApplicationHost owns durable scheduling, SQLite and durable workers.
    # A Web RuntimeManager may retain it after this Session closes.
    application_host: ApplicationHost | None = None
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


def runtime_config_from_args(args: argparse.Namespace) -> RuntimeConfig:
    """Translate an entry-point namespace once; assembly never parses CLI data."""
    hooks = getattr(args, "hooks_config", None)
    return RuntimeConfig(
        workspace=getattr(args, "workspace", None),
        resume=getattr(args, "resume", None),
        continue_latest=bool(getattr(args, "continue_latest", False)),
        no_session_persistence=bool(getattr(args, "no_session_persistence", False)),
        hooks_config=Path(hooks) if hooks else None,
        model=getattr(args, "model", None),
        transport=getattr(args, "transport", None),
        trust_project_mcp=bool(getattr(args, "trust_project_mcp", False)),
    )


def parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Wright coding agent")
    resume_group = parser.add_mutually_exclusive_group()
    resume_group.add_argument(
        "--resume",
        nargs="?",
        const="",
        metavar="SESSION_ID",
        help="从指定 session checkpoint 恢复 (留空则列出历史菜单选择)",
    )
    resume_group.add_argument(
        "-c",
        "--continue",
        dest="continue_latest",
        action="store_true",
        help="恢复最近保存的 session checkpoint",
    )
    parser.add_argument(
        "--no-session-persistence",
        action="store_true",
        help="本次运行不保存 checkpoint",
    )
    parser.add_argument(
        "--hooks-config",
        metavar="PATH",
        help="显式启用指定 lifecycle command hooks 配置（不会自动执行仓库配置）",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        metavar="DIR",
        help="要编辑的项目目录 (默认: 当前工作目录)",
    )
    parser.add_argument(
        "--ui",
        choices=("cli", "tui", "web", "headless"),
        default="tui",
        help="界面：tui（默认）、cli、本机 Web 控制台或 headless 自动任务宿主",
    )
    parser.add_argument(
        "--automation-session",
        metavar="SESSION_ID",
        help="headless 宿主要承载的 Automation 来源 session_id（不会扫描历史项目）",
    )
    parser.add_argument(
        "--model",
        metavar="MODEL",
        help="覆盖 OPENAI_MODEL；TUI 的 /model 使用同一配置",
    )
    parser.add_argument(
        "--transport",
        choices=("auto", "chat", "responses"),
        default=None,
        help="主模型协议：auto（默认）、chat 或 responses",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=0,
        metavar="PORT",
        help="Web 控制台端口（默认 0：自动选择空闲端口）",
    )
    parser.add_argument(
        "--web-capacity",
        type=int,
        default=4,
        metavar="N",
        help="Web 活跃 session 上限（默认 4）",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="启动 Web 控制台时不自动打开浏览器",
    )
    parser.add_argument(
        "--trust-project-mcp",
        action="store_true",
        help="允许启动项目 .wright/mcp.json 中声明的进程或远程连接",
    )
    return parser.parse_args()


def _make_interaction_handler(renderer: Renderer):
    """ask_user 的交互 adapter 工厂：UI 委托给 renderer，这里只做「原始回答 → PermissionCheckResult」的翻译。"""

    def handler(request: PermissionRequest) -> PermissionCheckResult:
        arguments = request.arguments
        answer = renderer.prompt_user(
            question=arguments["question"].strip(),
            context=arguments.get("context", "").strip(),
            options=tuple(arguments.get("options") or ()),
        )
        if answer is None:
            return PermissionCheckResult(
                "deny",
                "用户取消回答问题",
                request.check.risk_flags,
                source="user_interaction",
            )
        return PermissionCheckResult(
            "allow",
            "用户已回答问题",
            request.check.risk_flags,
            updated_arguments={**arguments, "answer": answer},
            source="user_interaction",
        )

    return handler


def load_env() -> None:
    """Load API keys from the nearest .env without requiring a specific cwd."""
    here = Path(__file__).resolve()
    candidates = [
        Path.cwd() / ".env",
        here.parents[2] / ".env",  # repo root (src/wright/runtime.py)
        here.parent / ".env",
    ]
    for path in candidates:
        if path.is_file():
            load_dotenv(path)
            return
    load_dotenv()


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


def assemble_runtime(
    config: RuntimeConfig,
    *,
    renderer: Renderer | None = None,
    project_context: ProjectContext | None = None,
    publisher: EventPublisher | None = None,
    interaction_broker: Any = None,
    session_id: str | None = None,
    start_automation: bool = True,
    automation_session_id: str | None = None,
    application_host: ApplicationHost | None = None,
) -> WrightRuntime:
    load_env()

    base_url = os.getenv("OPENAI_BASE_URL")
    api_key = os.getenv("OPENAI_API_KEY")
    requested_model = config.model
    configured_model = os.getenv("OPENAI_MODEL")
    context_limit_raw = os.getenv("OPENAI_CONTEXT_LIMIT")
    context_limit = int(context_limit_raw) if context_limit_raw else None

    if renderer is None:
        renderer = ConsoleRenderer()
        renderer.bind_interaction(InteractionHub())

    requested_workspace = (config.workspace or Path.cwd()).expanduser().resolve()
    project_context = project_context or ProjectContext.local(requested_workspace)
    workspace_dir = project_context.execution_root
    project_root = project_context.project_root
    if not workspace_dir.is_dir():
        raise SystemExit(f"workspace 不存在: {workspace_dir}")
    ensure_project_state(project_root)
    logger.info("workspace=%s", workspace_dir)
    logger.info("state=%s", session_dir(project_root).parent)

    # 多轮对话:session 整段存活,每轮把用户输入 append 进同一条历史；Agent.run 会把
    # user_goal 更新为当前任务，供 Verifier 和 checkpoint 使用。
    checkpoint_store = SessionCheckpointStore(session_dir(project_root))
    resume_arg = config.resume
    continue_latest = config.continue_latest
    resumed = bool(resume_arg is not None or continue_latest)
    try:
        if resume_arg is not None:
            resume_id = resume_arg.strip()
            if not resume_id:
                recent = checkpoint_store.list_recent_sessions(limit=5)
                if not recent:
                    raise CheckpointError("没有找到任何可恢复的历史 checkpoint")
                print("\n请选择要恢复的历史会话：")
                for i, item in enumerate(recent, 1):
                    goal = item["user_goal"] or "(无目标描述)"
                    if len(goal) > 40:
                        goal = goal[:37] + "..."
                    print(
                        f"  [{i}] {item['saved_at']} ({item['session_id']}) "
                        f'| "{goal}" (status: {item["status"]})'
                    )
                print()
                choice_str = prompt("输入序号 (默认 [1]): ").strip()
                idx = 0
                if choice_str:
                    try:
                        idx = int(choice_str) - 1
                    except ValueError:
                        raise CheckpointError(f"无效的选择: {choice_str}") from None
                if not (0 <= idx < len(recent)):
                    raise CheckpointError(f"选择超出范围: {choice_str}")
                resume_id = recent[idx]["session_id"]
            session_state = checkpoint_store.load(resume_id)
        elif continue_latest:
            session_state = checkpoint_store.load_latest()
        else:
            session_state = Session.create(
                initial_goal="(interactive session)",
                workspace_dir=workspace_dir,
                session_id=session_id,
                project_root=project_root,
                environment=project_context.environment,
                base_commit=project_context.base_commit,
                branch_name=project_context.branch_name,
            )
    except CheckpointError as exc:
        raise SystemExit(f"无法恢复会话: {exc}") from exc

    # An explicit --model wins. Otherwise resuming retains the model selected
    # in that session, falling back to OPENAI_MODEL for new/legacy sessions.
    model = requested_model or session_state.model_name or configured_model
    if not model:
        raise ValueError("OPENAI_MODEL 或 --model 不能为空")
    session_state.model_name = model
    attachment_store = AttachmentStore(attachment_dir(project_root), session_state.session_id)
    artifact_store = ArtifactStore(artifact_dir(project_root))
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
    )
    session_state.llm_transport = llm_client.transport_name
    llm_client.session_attachments = session_state.attachments
    draft_attachments = DraftAttachments(attachment_store, session_state.attachments)
    runtime_resources = RuntimeResources.for_session(session_state.session_id)
    assert runtime_resources is not None

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

    event_queue: queue.Queue[tuple[str, object]] = queue.Queue()
    cancellation_event = threading.Event()
    # agent_idle：loop 调度与主输入框都等它；忙碌时不画「你的指令」。
    agent_idle = threading.Event()
    agent_idle.set()
    background_runtime = AgentBackgroundRuntime(event_queue)
    autonomy_store = AutonomyStore(
        task_db_path(project_root),
        session_id=automation_session_id or session_state.session_id,
        workspace_dir=workspace_dir,
    )
    # A resumed Web session can rebind its pre-existing application owner.
    # Its durable store is the durable fact source; do not create a competing
    # scheduler/owner merely to reconstruct a conversation runtime.
    if application_host is not None:
        autonomy_store.close()
        autonomy_store = application_host.store
    loop_registry = SessionLoopRegistry(event_queue, agent_idle)
    services = RuntimeServices(
        agent_background=background_runtime,
        durable_store=autonomy_store,
        loop_registry=loop_registry,
    )
    interaction_target = interaction_broker or getattr(renderer, "_hub", None)
    if interaction_target is not None:
        bind_persistence = getattr(interaction_target, "set_persistence", None)
        if callable(bind_persistence):
            interaction_scope = f"session:{session_state.session_id}"

            def record_interaction(request_id, kind, payload) -> None:
                active = session_state.active_run()
                autonomy_store.record_interaction(
                    interaction_scope, request_id, kind=str(kind), payload=payload,
                    run_id=active.run_id if active is not None else "",
                )

            def resolve_interaction(request_id, _kind, resolution) -> None:
                autonomy_store.resolve_interaction(
                    interaction_scope, request_id, resolution=resolution,
                    status="cancelled" if resolution.get("cancelled") else "resolved",
                )

            bind_persistence(record_interaction, resolve_interaction)
    mcp_manager: McpManager | None = None
    constructed_application_host: ApplicationHost | None = application_host
    own_publisher = publisher is None

    def abort_assembly() -> None:
        """Release resources constructed before a runtime became publishable."""
        cancellation_event.set()
        cancelled_tasks = runtime_resources.close()
        session_state.mark_background_tasks_cancel_requested(
            cancelled_tasks, "runtime assembly failed"
        )
        if interaction_broker is not None:
            interaction_broker.close()
        try:
            loop_registry.close()
        finally:
            try:
                if constructed_application_host is not None and application_host is None:
                    constructed_application_host.close()
            finally:
                try:
                    background_runtime.shutdown(session_state.control_plane)
                finally:
                    try:
                        if application_host is None:
                            autonomy_store.close()
                    finally:
                        if mcp_manager is not None:
                            mcp_manager.shutdown()
                        if own_publisher and publisher is not None:
                            publisher.close()
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
        raise SystemExit(f"无法加载 lifecycle hooks: {exc}") from exc
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
            "忽略未受信任的项目 MCP 配置 %s；需要时使用 --trust-project-mcp",
            ignored_project_mcp,
        )
    try:
        mcp_configs = load_mcp_configs(mcp_paths)
        mcp_manager = McpManager(mcp_configs, artifact_store=artifact_store)
        mcp_tools = mcp_manager.start()
    except Exception:
        abort_assembly()
        raise

    publisher = publisher or EventPublisher(
        project_id=project_id(project_root),
        session_id=session_state.session_id,
    )
    event_renderer = PublishingRenderer(
        publisher,
        interaction=interaction_broker,
        direct_renderer=renderer,
        runtime_resources=runtime_resources,
    )

    # 权限裁决:加载持久化配置(模式 + allow/deny 规则),按"要不要人"两种装配。
    #
    # 要不要人,默认看有没有真终端,不用记环境变量(env 仍可强制覆盖):
    #   - 有 TTY(你坐在终端前) → 规则 + 人:规则 on_no_match=ask 对灰色地带"弃权",
    #     落到交互式 handler 弹窗问你;rm/sudo 等 deny 仍直接拒、不打扰你。
    #   - 无 TTY(管道/CI/后台) → 纯规则,on_no_match=deny 直接 fail-closed,绝不阻塞。
    # 关键:能不能被问到,取决于规则有没有提前 allow 它——allow 列得越全,落到人手里越少。
    # 只读工具保留自身的 allow 分类；默认配置不再用宽泛命令前缀放行 shell。
    # 写文件、网络和 shell 等副作用调用会落到规则或人工确认。
    # 主 Agent 与所有子 Agent 共用这同一份 resolver,规则/记忆全树一致。
    settings = load_permission_settings()
    env_interactive = os.getenv("WRIGHT_PERMISSION_INTERACTIVE")
    interactive = interaction_broker is not None or (
        env_interactive == "1" if env_interactive is not None else sys.stdin.isatty()
    )
    if interactive:
        approval_handler = FallbackApprovalHandler(
            RuleBasedApprovalHandler(settings, on_no_match="ask"),
            # on_remember:用户选"别再问"时把规则写回 settings.json,下次同工具在规则层
            # 就自动放行(连这个交互 handler 都到不了)——对标 Claude Code 的"Yes, don't ask again"。
            InteractiveApprovalHandler(renderer=event_renderer, on_remember=append_allow_rule),
        )
    else:
        approval_handler = RuleBasedApprovalHandler(settings)
    permission_resolver = PermissionResolver(
        approval_handler=approval_handler,
        interaction_handler=(
            _make_interaction_handler(event_renderer) if interactive else None
        ),
    )

    # 给主 Agent 装上"基础工具 + spawn_agent"的分层工具集:depth=0 是主 Agent,
    # 默认只委派叶子子 Agent（max_depth=1）。嵌套 spawn 仍由控制面支持，
    # 无人值守 durable run 会显式打开第二层。
    # knowledge_search 是只读检索：启用后放进 base，让子 Agent 也能查知识库。
    knowledge_tools = optional_knowledge_tools()
    assembled_base = [
        *base_tools,
        *(replace(tool, defer_to_model=True) for tool in mcp_tools),
        *knowledge_tools,
    ]
    tools = build_agent_tools(
        llm_client,
        assembled_base,
        depth=0,
        max_depth=1,
        permission_resolver=permission_resolver,
        enable_autonomy=True,
    )

    # ask_user、manage_loop、记忆与 load_skill 只给主 Agent:都在 build_agent_tools 之后
    # 【单独追加】，不进 base_tools。子 Agent 不能绕过父 Agent 直接打断人；
    # 它若信息不足，应把缺口作为结果交回父 Agent。子 Agent 也保持无长期记忆、
    # 无 skill 加载器的纯净上下文——委派时把需要的流程写进任务描述。
    # loop 同理：会话内重跑必须看见当前对话，不能下放到隔离的子 Agent。
    memory_manager = MemoryManager(llm_client, selector_llm=selector_llm)
    skill_registry = SkillRegistry(skill_directories(workspace_dir))
    skill_tools = [
        replace(tool, defer_to_model=True)
        for tool in optional_skill_tools(skill_registry)
    ]
    tools = [
        *tools,
        ask_user_tool,
        manage_loop_tool,
        *memory_manager.tools(),
        *skill_tools,
    ]

    agent = Agent(
        llm_client,
        tools,
        session_state,
        event_renderer,
        keep_recent_tool_results=3,
        permission_resolver=permission_resolver,
        memory=memory_manager,
        verifier=Verifier(),
        checkpoint_store=(
            None
            if config.no_session_persistence
            else checkpoint_store
        ),
        on_shell_task_done=lambda task_id: event_queue.put(("TASK_DONE", task_id)),
        lifecycle=lifecycle,
        skills=skill_registry if skill_tools else None,
        services=services,
        runtime_resources=runtime_resources,
    )

    # Automation is process/application owned, not consumed by this Session's
    # event worker.  The foreground Session keeps its own subagent runtime but
    # all scheduling tools resolve this host scheduler through RuntimeServices.
    if constructed_application_host is None:
        constructed_application_host = ApplicationHost(
            workspace_dir=workspace_dir,
            store=autonomy_store,
            llm=llm_client,
            # Host builds its own MCP wrappers and never retains tools bound
            # to the Session-owned manager above.
            base_tools=[*base_tools, *knowledge_tools],
            permission_settings=settings,
            mcp_configs=mcp_configs,
            artifact_store=artifact_store,
        )
    services.autonomy_scheduler = constructed_application_host.scheduler

    try:
        if start_automation:
            constructed_application_host.start()
        loop_registry.start()
    except Exception:
        abort_assembly()
        raise

    return WrightRuntime(
        agent=agent,
        session_state=session_state,
        renderer=renderer,
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
        permission_settings=settings,
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
    )


def build_runtime(
    args: argparse.Namespace,
    **kwargs: Any,
) -> WrightRuntime:
    """Legacy argparse wrapper; production hosts call ``assemble_runtime``."""
    return assemble_runtime(runtime_config_from_args(args), **kwargs)


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
    rt.session_state.mark_background_tasks_cancel_requested(
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
