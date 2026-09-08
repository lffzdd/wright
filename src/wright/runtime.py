"""Assemble the process-local Wright runtime used by the terminal REPL."""

from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from prompt_toolkit import prompt

from .agent import Agent
from .agent_background import AgentBackgroundRuntime
from .autonomy import AutonomyScheduler, AutonomyStore
from .checkpoint import CheckpointError, SessionCheckpointStore
from .interaction import InteractionHub
from .knowledge import optional_knowledge_tools
from .lifecycle import LifecycleConfigError, load_lifecycle_manager
from .llm import LLMClient
from .logger import get_logger
from .looping import SessionLoopRegistry
from .memory import MemoryManager
from .paths import (
    ensure_project_state,
    mcp_config_paths,
    session_dir,
    skill_directories,
    task_db_path,
    trace_dir,
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
from .renderer import ConsoleRenderer, Renderer
from .services import RuntimeServices
from .session import SessionState
from .skills import SkillRegistry, optional_skill_tools
from .subagent import build_agent_tools
from .tools import tools as base_tools
from .tools.ask_user_tool import ask_user_tool
from .tools.base import Tool
from .tools.loop_tools import loop_tool
from .tools.mcp_client import McpManager, load_mcp_configs
from .verifier import Verifier

logger = get_logger(__name__)


@dataclass(frozen=True)
class WrightRuntime:
    agent: Agent
    session_state: SessionState
    renderer: Renderer
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
        choices=("cli", "tui"),
        default="cli",
        help="界面：cli 为主缓冲经典终端（默认），tui 为全屏备用缓冲区",
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


def _load_env() -> None:
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


def build_runtime(
    args: argparse.Namespace,
    *,
    renderer: Renderer | None = None,
) -> WrightRuntime:
    _load_env()

    base_url = os.getenv("OPENAI_BASE_URL")
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL")
    context_limit_raw = os.getenv("OPENAI_CONTEXT_LIMIT")
    context_limit = int(context_limit_raw) if context_limit_raw else None

    llm_client = LLMClient(
        base_url=base_url,
        api_key=api_key,
        model=model,
        context_limit=context_limit or 128000,
    )

    # 记忆的召回/提取 side-query 可选用更便宜的模型省钱(对标 memdir 用 Sonnet 选记忆)。
    # 配了 OPENAI_MEMORY_MODEL 就单独建个非流式 client,否则复用主 client。
    memory_model = os.getenv("OPENAI_MEMORY_MODEL")
    selector_llm = (
        LLMClient(
            base_url=base_url,
            api_key=api_key,
            model=memory_model,
            stream=False,
        )
        if memory_model
        else llm_client
    )

    if renderer is None:
        renderer = ConsoleRenderer()
        renderer.bind_interaction(InteractionHub())

    workspace_dir = (args.workspace or Path.cwd()).expanduser().resolve()
    if not workspace_dir.is_dir():
        raise SystemExit(f"workspace 不存在: {workspace_dir}")
    ensure_project_state(workspace_dir)
    logger.info("workspace=%s", workspace_dir)
    logger.info("state=%s", session_dir(workspace_dir).parent)

    # 多轮对话:session 整段存活,每轮把用户输入 append 进同一条历史；Agent.run 会把
    # user_goal 更新为当前任务，供 Verifier 和 checkpoint 使用。
    checkpoint_store = SessionCheckpointStore(session_dir(workspace_dir))
    resumed = bool(args.resume is not None or args.continue_latest)
    try:
        if args.resume is not None:
            session_id = args.resume.strip()
            if not session_id:
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
                session_id = recent[idx]["session_id"]
            session_state = checkpoint_store.load(session_id)
        elif args.continue_latest:
            session_state = checkpoint_store.load_latest()
        else:
            session_state = SessionState.create(
                user_goal="(interactive session)",
                workspace_dir=workspace_dir,
            )
    except CheckpointError as exc:
        raise SystemExit(f"无法恢复会话: {exc}") from exc

    event_queue: queue.Queue[tuple[str, object]] = queue.Queue()
    # agent_idle：loop 调度与主输入框都等它；忙碌时不画「你的指令」。
    agent_idle = threading.Event()
    agent_idle.set()
    background_runtime = AgentBackgroundRuntime(event_queue)
    autonomy_store = AutonomyStore(
        task_db_path(workspace_dir),
        session_id=session_state.session_id,
        workspace_dir=workspace_dir,
    )
    autonomy_scheduler = AutonomyScheduler(autonomy_store, event_queue)
    loop_registry = SessionLoopRegistry(event_queue, agent_idle)
    services = RuntimeServices(
        agent_background=background_runtime,
        durable_store=autonomy_store,
        autonomy_scheduler=autonomy_scheduler,
        loop_registry=loop_registry,
    )
    try:
        lifecycle = load_lifecycle_manager(
            workspace_dir,
            session_state.session_id,
            config_path=(Path(args.hooks_config) if args.hooks_config else None),
            trace_dir=trace_dir(workspace_dir),
        )
    except LifecycleConfigError as exc:
        raise SystemExit(f"无法加载 lifecycle hooks: {exc}") from exc
    lifecycle.emit(
        "session_start",
        {"resumed": resumed, "workspace_dir": str(workspace_dir)},
        root_turn_id=session_state.agent_root_turn_id,
    )

    # MCP 接入:用户 ~/.wright/mcp.json + 项目 .wright/mcp.json,后者同名覆盖。
    # 没配时 configs 为空,start() 直接返回 [],对其余流程完全无感。
    mcp_manager = McpManager(load_mcp_configs(mcp_config_paths(workspace_dir)))
    mcp_tools = mcp_manager.start()

    # 权限裁决:加载持久化配置(模式 + allow/deny 规则),按"要不要人"两种装配。
    #
    # 要不要人,默认看有没有真终端,不用记环境变量(env 仍可强制覆盖):
    #   - 有 TTY(你坐在终端前) → 规则 + 人:规则 on_no_match=ask 对灰色地带"弃权",
    #     落到交互式 handler 弹窗问你;rm/sudo 等 deny 仍直接拒、不打扰你。
    #   - 无 TTY(管道/CI/后台) → 纯规则,on_no_match=deny 直接 fail-closed,绝不阻塞。
    # 关键:能不能被问到,取决于规则有没有提前 allow 它——allow 列得越全,落到人手里越少。
    # 默认配置只 allow 只读命令,所以写文件/网络/python 都会落到你这来确认。
    # 主 Agent 与所有子 Agent 共用这同一份 resolver,规则/记忆全树一致。
    settings = load_permission_settings()
    env_interactive = os.getenv("WRIGHT_PERMISSION_INTERACTIVE")
    interactive = (
        env_interactive == "1"
        if env_interactive is not None
        else sys.stdin.isatty()
    )
    if interactive:
        approval_handler = FallbackApprovalHandler(
            RuleBasedApprovalHandler(settings, on_no_match="ask"),
            # on_remember:用户选"别再问"时把规则写回 settings.json,下次同工具在规则层
            # 就自动放行(连这个交互 handler 都到不了)——对标 Claude Code 的"Yes, don't ask again"。
            InteractiveApprovalHandler(renderer=renderer, on_remember=append_allow_rule),
        )
    else:
        approval_handler = RuleBasedApprovalHandler(settings)
    permission_resolver = PermissionResolver(
        approval_handler=approval_handler,
        interaction_handler=_make_interaction_handler(renderer) if interactive else None,
    )

    # 给主 Agent 装上"基础工具 + spawn_agent"的分层工具集:depth=0 是主 Agent,
    # 默认只委派叶子子 Agent（max_depth=1）。嵌套 spawn 仍由控制面支持，
    # 无人值守 durable run 会显式打开第二层。
    # knowledge_search 是只读检索：启用后放进 base，让子 Agent 也能查知识库。
    knowledge_tools = optional_knowledge_tools()
    assembled_base = base_tools + mcp_tools + knowledge_tools
    tools = build_agent_tools(
        llm_client,
        assembled_base,
        depth=0,
        max_depth=1,
        permission_resolver=permission_resolver,
        enable_autonomy=True,
    )

    # ask_user、loop、记忆与 skill 工具只给主 Agent:都在 build_agent_tools 之后
    # 【单独追加】，不进 base_tools。子 Agent 不能绕过父 Agent 直接打断人；
    # 它若信息不足，应把缺口作为结果交回父 Agent。子 Agent 也保持无长期记忆、
    # 无 skill 加载器的纯净上下文——委派时把需要的流程写进任务描述。
    # loop 同理：会话内重跑必须看见当前对话，不能下放到隔离的子 Agent。
    memory_manager = MemoryManager(llm_client, selector_llm=selector_llm)
    skill_registry = SkillRegistry(skill_directories(workspace_dir))
    skill_tools = optional_skill_tools(skill_registry)
    tools = [
        *tools,
        ask_user_tool,
        loop_tool,
        *memory_manager.tools(),
        *skill_tools,
    ]

    agent = Agent(
        llm_client,
        tools,
        session_state,
        renderer,
        keep_recent_tool_results=3,
        permission_resolver=permission_resolver,
        memory=memory_manager,
        verifier=Verifier(),
        checkpoint_store=(None if args.no_session_persistence else checkpoint_store),
        on_shell_task_done=lambda task_id: event_queue.put(("TASK_DONE", task_id)),
        lifecycle=lifecycle,
        skills=skill_registry if skill_tools else None,
        services=services,
    )

    autonomy_scheduler.start()
    loop_registry.start()

    return WrightRuntime(
        agent=agent,
        session_state=session_state,
        renderer=renderer,
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
    )


def shutdown_runtime(rt: WrightRuntime) -> None:
    if rt.services.loop_registry is not None:
        rt.services.loop_registry.close()
    if rt.services.autonomy_scheduler is not None:
        rt.services.autonomy_scheduler.close()
    if rt.services.agent_background is not None:
        rt.services.agent_background.shutdown(rt.session_state.control_plane)
    if rt.services.durable_store is not None:
        rt.services.durable_store.close()
    # 关闭 MCP session / stdio 子进程,避免残留进程。
    rt.mcp_manager.shutdown()
