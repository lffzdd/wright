"""
Wright 主入口：终端里的 coding agent。

当前回合协议仍是多工具 ReAct JSON 信封；主循环一次可发起多个工具调用。
"""

import argparse
import json
import os
import queue
import sys
import threading
from pathlib import Path

from dotenv import load_dotenv
from prompt_toolkit import PromptSession, prompt

from .agent import Agent
from .agent_background import AgentBackgroundRuntime
from .autonomy import AutonomyScheduler, AutonomyStore, AutonomyStoreError
from .autonomy.runner import launch_durable_run
from .checkpoint import CheckpointError, SessionCheckpointStore
from .knowledge import optional_knowledge_tools
from .logger import get_logger
from .memory import MemoryManager
from .tools import tools as base_tools
from .tools.ask_user_tool import ask_user_tool
from .tools.loop_tools import loop_tool
from .llm import LLMClient
from .lifecycle import LifecycleConfigError, load_lifecycle_manager
from .looping import SessionLoopRegistry, parse_loop_command
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
    RuleBasedApprovalHandler,
    append_allow_rule,
    load_permission_settings,
)
from .renderer import ConsoleRenderer
from .session import SessionState
from .skills import SkillRegistry, optional_skill_tools
from .subagent import build_agent_tools
from .tasks import RuntimeTask, TaskNotFoundError, TaskService
from .tools.mcp_client import McpManager, load_mcp_configs
from .verifier import Verifier


logger = get_logger(__name__)


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
    return parser.parse_args()


def _make_interaction_handler(renderer: ConsoleRenderer):
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


def _task_notification_event(task: RuntimeTask) -> dict:
    """Adapt a RuntimeTask into a runtime-event envelope for agent.run_runtime_event()."""
    return {
        "type": "task_notification",
        "task": {
            "id": task.id[:100],
            "kind": task.kind,
            "root_turn_id": task.root_turn_id[:180],
            "status": task.status,
            "description": task.description[:500],
            "result": task.result[:2_000],
            "output": task.output[-2_000:],
            "error": task.error[:1_000],
            "returncode": task.returncode,
            "cancel_requested": task.cancel_requested,
            "cancel_reason": task.cancel_reason[:500],
        },
    }


def _render_durable_run_finished(store: AutonomyStore, run_id: str) -> None:
    """只打一行摘要，不注入 root 上下文、不跑 Agent turn。"""
    try:
        run = store.get_run(run_id)
    except AutonomyStoreError:
        print(f"⏰ durable run {run_id} finished")
        return
    preview = (run.result or run.error or "").replace("\n", " ").strip()
    if len(preview) > 120:
        preview = preview[:117] + "..."
    detail = f": {preview}" if preview else ""
    print(
        f"⏰ durable run {run.automation_name} [{run.status}] "
        f"({run.id}){detail}"
    )


def _parse_external_event_command(value: str) -> tuple[str, dict]:
    parts = value.strip().split(maxsplit=2)
    if len(parts) < 2:
        raise ValueError("用法: /event <name> [JSON object]")
    payload: dict = {}
    if len(parts) == 3:
        parsed = json.loads(parts[2])
        if not isinstance(parsed, dict):
            raise ValueError("event payload 必须是 JSON object")
        payload = parsed
    return parts[1], payload


def _handle_loop_command(value: str, registry: SessionLoopRegistry) -> None:
    action, payload = parse_loop_command(value)
    if action == "list":
        records = registry.list_loops()
        if not records:
            print("没有运行中的 loop")
            return
        for record in records:
            print(
                f"  {record.id}  every {record.interval_seconds:g}s  "
                f"tick={record.tick_count}  {record.name}"
            )
        return
    if action == "stop":
        record = registry.stop(str(payload))
        print(f"已停止 loop {record.id} ({record.name})")
        return
    interval, prompt = payload
    record = registry.create(prompt=prompt, interval_seconds=interval)
    print(
        f"🔁 loop {record.id} every {record.interval_seconds:g}s: {record.prompt}"
    )


def _start_input_reader(
    renderer: ConsoleRenderer,
    event_queue: "queue.Queue[tuple[str, object]]",
    agent_idle: threading.Event,
) -> threading.Thread:
    """Keep terminal input blocking away from the session event consumer.

    agent_idle 由主线程维护：
      - agent.run() 结束后 set()（空闲）
      - agent.run() 开始前 clear()（忙碌）

    输入线程在【渲染提示符之前】先 wait()，确保 agent 空闲后才让用户看到输
    入框；用户提交后立刻 clear() 再 put()，避免主线程来不及 clear 就被下一
    次 wait() 穿透的竞态。
    """
    def read() -> None:
        prompt_session: PromptSession[str] = PromptSession()
        while True:
            # 先等 agent 空闲，再渲染输入提示符——保证提示符不会出现在回答中间
            agent_idle.wait()
            value = renderer.prompt_main_input(prompt_session)
            if value is None or value in {"/exit", "/quit"}:
                event_queue.put(("EXIT", None))
                return
            if value:
                # clear() 必须在 put() 之前：主线程 get() 后才 clear，
                # 若放在 put() 后则 wait() 可能在主线程 clear() 前就穿透。
                agent_idle.clear()
                event_queue.put(("USER_INPUT", value))

    thread = threading.Thread(target=read, name="wright-input", daemon=True)
    thread.start()
    return thread



def _load_env() -> None:
    """Load API keys from the nearest .env without requiring a specific cwd."""
    here = Path(__file__).resolve()
    candidates = [
        Path.cwd() / ".env",
        here.parents[2] / ".env",  # repo root (src/wright/main.py)
        here.parent / ".env",
    ]
    for path in candidates:
        if path.is_file():
            load_dotenv(path)
            return
    load_dotenv()


def main() -> None:
    args = parse_cli_args()
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

    verifier_model = os.getenv("OPENAI_VERIFIER_MODEL")
    verifier_llm = (
        LLMClient(
            base_url=base_url,
            api_key=api_key,
            model=verifier_model,
            stream=False,
        )
        if verifier_model
        else selector_llm
    )

    renderer = ConsoleRenderer()

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
                        raise CheckpointError(f"无效的选择: {choice_str}")
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

    event_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
    # agent_idle 提前创建：loop 调度线程和输入线程都要等它。
    agent_idle = threading.Event()
    agent_idle.set()
    background_runtime = AgentBackgroundRuntime(event_queue)
    session_state.agent_background_runtime = background_runtime
    autonomy_store = AutonomyStore(
        task_db_path(workspace_dir),
        session_id=session_state.session_id,
        workspace_dir=workspace_dir,
    )
    autonomy_scheduler = AutonomyScheduler(autonomy_store, event_queue)
    session_state.durable_task_store = autonomy_store
    session_state.autonomy_scheduler = autonomy_scheduler
    loop_registry = SessionLoopRegistry(event_queue, agent_idle)
    session_state.loop_registry = loop_registry
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
    # 它能委派出 depth=1 的子 Agent;到 max_depth 那层不再带 spawn,递归到底。
    # knowledge_search 是只读检索：启用后放进 base，让子 Agent 也能查知识库。
    knowledge_tools = optional_knowledge_tools()
    assembled_base = base_tools + mcp_tools + knowledge_tools
    tools = build_agent_tools(
        llm_client,
        assembled_base,
        depth=0,
        max_depth=2,
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
        verifier=Verifier(verifier_llm),
        checkpoint_store=(None if args.no_session_persistence else checkpoint_store),
        on_shell_task_done=lambda task_id: event_queue.put(("TASK_DONE", task_id)),
        lifecycle=lifecycle,
        skills=skill_registry if skill_tools else None,
    )

    autonomy_scheduler.start()
    loop_registry.start()

    # ---- Event-driven REPL ----
    # 只有这个循环会改 root SessionState。durable run 在这里构造独立会话后
    # 丢给后台，完成时只渲染摘要，不再走 run_runtime_event。
    #
    # agent_idle：输入线程、loop 调度线程与主线程之间的同步信号。
    #   主线程在 agent.run() 前 clear()，结束后 set()；
    #   输入线程 put 完事件后 wait()，确保下一个提示符在回答渲染完毕后才出现。
    #   loop 只在 set()（空闲）时投递 LOOP_DUE，忙时错过的周期合并成一次。
    try:
        if resumed:
            print(
                f"已恢复 session {session_state.session_id} "
                f"(status={session_state.status})"
            )
            renderer.render_session_history(session_state)
            if session_state.status == "running":
                agent.continue_run()
        _start_input_reader(renderer, event_queue, agent_idle)
        while True:
            event_type, payload = event_queue.get()
            if event_type == "EXIT":
                break
            if event_type == "USER_INPUT":
                # agent_idle 已由输入线程在 put() 前 clear()，无需重复 clear
                user_input = str(payload)

                # ── 斜杠命令拦截（不占 agent step，不进 context） ──
                if user_input.strip().lower().startswith("/history"):
                    arg = user_input.strip()[len("/history"):].strip().lower()
                    if arg == "all":
                        renderer.render_session_history(session_state, pager=True)
                    elif arg.isdigit():
                        renderer.render_session_history(
                            session_state, max_turns=int(arg)
                        )
                    else:
                        renderer.render_session_history(session_state)
                    agent_idle.set()
                    continue

                if user_input.strip().lower().startswith("/event"):
                    try:
                        event_name, event_payload = _parse_external_event_command(
                            user_input
                        )
                        event_id = autonomy_scheduler.emit_event(
                            event_name, event_payload
                        )
                        print(f"📨 external event accepted (id={event_id})")
                    except Exception as exc:
                        print(f"external event rejected: {exc}")
                    agent_idle.set()
                    continue

                if user_input.strip().lower().startswith("/loop"):
                    try:
                        _handle_loop_command(user_input, loop_registry)
                    except Exception as exc:
                        print(f"loop rejected: {exc}")
                    agent_idle.set()
                    continue

                try:
                    agent.run(user_input)
                finally:
                    agent_idle.set()

            elif event_type == "TASK_DONE":
                # Agent/Shell 都只发送 task_id；主线程从唯一真实 owner 投影出
                # 同一种 RuntimeTask 后再唤醒 Agent。
                agent_idle.clear()
                try:
                    try:
                        task = TaskService.for_session(session_state).get(str(payload))
                    except TaskNotFoundError:
                        logger.warning("忽略未知后台任务完成事件: %s", payload)
                    else:
                        agent.run_runtime_event(_task_notification_event(task))
                finally:
                    agent_idle.set()

            elif event_type == "DURABLE_RUN_DUE":
                # 只派发，不阻塞事件循环。构造 run 仍只允许本线程。
                try:
                    launch_durable_run(
                        run_id=str(payload),
                        root_session=session_state,
                        scheduler=autonomy_scheduler,
                        llm=llm_client,
                        base_tools=assembled_base,
                        permission_settings=settings,
                        background_runtime=background_runtime,
                        lifecycle=lifecycle,
                    )
                except Exception:
                    logger.exception("durable task dispatch failed: %s", payload)

            elif event_type == "DURABLE_RUN_FINISHED":
                _render_durable_run_finished(autonomy_store, str(payload))

            elif event_type == "LOOP_DUE":
                agent_idle.clear()
                try:
                    record = loop_registry.begin_tick(str(payload))
                    if record is not None:
                        agent.run_runtime_event(loop_registry.runtime_event(record))
                except Exception:
                    logger.exception("session loop execution failed: %s", payload)
                finally:
                    loop_registry.finish_tick(str(payload))
                    agent_idle.set()

            elif event_type == "AUTONOMY_ERROR":
                logger.error("autonomy scheduler error: %s", payload)
        if agent.checkpoint_store:
            print(f"💾 会话已保存 (session_id: {session_state.session_id})")
    finally:
        loop_registry.close()
        autonomy_scheduler.close()
        background_runtime.shutdown(session_state.control_plane)
        autonomy_store.close()
        # 关闭 MCP session / stdio 子进程,避免残留进程。
        mcp_manager.shutdown()


if __name__ == "__main__":
    main()
