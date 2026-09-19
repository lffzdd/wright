import queue
import time

from wright.tests.responses import event, response

from ...agent import Agent
from ...agent_background import AgentBackgroundRuntime
from ...autonomy import AutonomyScheduler, AutonomyStore, TriggerSpec
from ...autonomy.runner import _DurableToolJournal, launch_durable_run
from ...capabilities import AgentProfile
from ...permission import PermissionSettings
from ...renderer import SilentRenderer
from ...services import RuntimeServices
from ...session import Session
from ...skills.registry import SkillRegistry
from ...skills.store import write_skill
from ...subagent import build_agent_tools
from ...tools.ask_user_tool import ask_user_tool
from ...tools.autonomy_tools import autonomy_tools
from ...tools.base import Tool, ToolResult
from ...tools.knowledge_tools import build_knowledge_tools
from ...tools.memory_tools import build_memory_tools
from ...tools.skill_tools import build_skill_tools


def _final(answer):
    return response(content=answer, calls=[])


class ScriptLLM:
    context_limit = 128_000

    def __init__(self, answers):
        self.answers = list(answers)

    def __call__(self, messages, **kwargs):
        yield event(_final(self.answers.pop(0)))


class SlowLLM:
    context_limit = 128_000

    def __init__(self, answer, delay=0.2):
        self.answer = answer
        self.delay = delay

    def __call__(self, messages, **kwargs):
        time.sleep(self.delay)
        yield event(_final(self.answer))


def _runtime(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = AutonomyStore(
        tmp_path / "tasks.sqlite3",
        session_id="session",
        workspace_dir=workspace,
    )
    events = queue.Queue()
    session = Session.create("interactive", workspace)
    session.session_id = "session"
    background = AgentBackgroundRuntime(events, max_workers=1)
    scheduler = AutonomyScheduler(store, events, poll_interval=1)
    services = RuntimeServices(
        agent_background=background,
        durable_store=store,
        autonomy_scheduler=scheduler,
    )
    return workspace, store, session, scheduler, events, background, services


def _claim_run(store, prompt="review the repository", name="daily review"):
    store.create_automation(
        name=name,
        prompt=prompt,
        trigger=TriggerSpec(type="once", run_at=0),
        now=0,
    )
    run_id = store.materialize_due(now=0)[0]
    claimed = store.claim_next_run(now=0)
    assert claimed is not None and claimed.id == run_id
    return run_id


def test_durable_run_leaves_root_session_untouched(tmp_path):
    workspace, store, session, scheduler, events, background, services = _runtime(tmp_path)
    nested = workspace / "nested"
    nested.mkdir()
    root_agent = Agent(
        ScriptLLM(["interactive result"]),
        [],
        session,
        SilentRenderer(),
    )
    assert root_agent.run("interactive turn") == "interactive result"
    session.plan_manager.create_plan("old", ["old step"])
    session.set_cwd(nested)
    root_goal = session.current_goal()
    root_plan = session.plan_manager.snapshot()
    root_len = len(session.message_records)
    root_cwd = session.get_cwd()
    root_user_contents = [
        record.message.get("content")
        for record in session.message_records
        if record.message.get("role") == "user"
    ]

    run_id = _claim_run(store)
    launch = launch_durable_run(
        run_id=run_id,
        root_session=session,
        scheduler=scheduler,
        llm=ScriptLLM(["autonomous result"]),
        base_tools=[],
        permission_settings=PermissionSettings(),
        background_runtime=background,
        services=services,
    )
    assert launch is not None
    event_type, finished_id = events.get(timeout=2)
    assert event_type == "DURABLE_RUN_FINISHED"
    assert finished_id == run_id

    assert session.current_goal() == root_goal
    assert session.plan_manager.snapshot() == root_plan
    assert len(session.message_records) == root_len
    assert session.get_cwd() == root_cwd

    durable_contents = [
        message.get("content") for message in launch.session.wire_messages()
    ]
    for content in root_user_contents:
        assert content not in durable_contents

    run = store.get_run(run_id)
    assert run.status == "completed"
    assert run.result == "autonomous result"
    assert run.root_turn_id == f"durable:{run_id}"
    background.shutdown(session.control_plane)
    store.close()


def test_durable_run_does_not_block_root_user_input(tmp_path):
    _workspace, store, session, scheduler, events, background, services = _runtime(tmp_path)
    run_id = _claim_run(store, prompt="slow work")
    started = time.monotonic()
    launch = launch_durable_run(
        run_id=run_id,
        root_session=session,
        scheduler=scheduler,
        llm=SlowLLM("autonomous result"),
        base_tools=[],
        permission_settings=PermissionSettings(),
        background_runtime=background,
        services=services,
    )
    assert launch is not None
    assert time.monotonic() - started < 0.08

    root_agent = Agent(
        ScriptLLM(["user heard"]),
        [],
        session,
        SilentRenderer(),
    )
    assert root_agent.run("please keep chatting") == "user heard"
    assert session.current_goal() == "please keep chatting"

    event_type, finished_id = events.get(timeout=2)
    assert event_type == "DURABLE_RUN_FINISHED"
    assert finished_id == run_id
    assert store.get_run(run_id).status == "completed"
    background.shutdown(session.control_plane)
    store.close()


def test_durable_session_omits_ask_user_and_autonomy_tools(tmp_path):
    _workspace, store, session, scheduler, events, background, services = _runtime(tmp_path)
    run_id = _claim_run(store)
    memory_tools = build_memory_tools(tmp_path / "memory")
    write_skill(
        tmp_path / "skills",
        "release-check",
        name="发布前检查",
        description="发布时使用",
        body="先跑测试",
    )

    class FakeKnowledge:
        def search(self, query, top_k):
            return []

    launch = launch_durable_run(
        run_id=run_id,
        root_session=session,
        scheduler=scheduler,
        llm=ScriptLLM(["done"]),
        base_tools=[
            ask_user_tool,
            *autonomy_tools,
            *memory_tools,
            *build_knowledge_tools(FakeKnowledge()),
            *build_skill_tools(SkillRegistry(tmp_path / "skills")),
        ],
        permission_settings=PermissionSettings(),
        background_runtime=background,
        services=services,
    )
    assert launch is not None
    names = set(launch.tool_names)
    assert "ask_user" not in names
    assert "create_task" not in names
    assert "schedule_task" not in names
    assert "pause_schedule" not in names
    assert "resume_schedule" not in names
    assert "cancel_schedule" not in names
    assert "get_schedule" not in names
    assert "list_schedules" not in names
    assert "list_task_runs" not in names
    assert "create_memory" not in names
    assert "search_memory" not in names
    assert "knowledge_search" not in names
    assert "skill" not in names
    assert "list_skills" not in names
    assert "load_skill" not in names
    assert "unload_skill" not in names
    events.get(timeout=2)
    background.shutdown(session.control_plane)
    store.close()


def test_cancelled_dispatched_run_is_not_started(tmp_path):
    _workspace, store, session, scheduler, _events, background, services = _runtime(tmp_path)
    run_id = _claim_run(store, prompt="should not execute", name="cancel me")
    store.cancel_run(run_id, "external cancellation")
    launch = launch_durable_run(
        run_id=run_id,
        root_session=session,
        scheduler=scheduler,
        llm=ScriptLLM([]),
        base_tools=[],
        permission_settings=PermissionSettings(),
        background_runtime=background,
        services=services,
    )
    assert launch is None
    run = store.get_run(run_id)
    assert run.status == "cancelled"
    assert run.cancel_reason == "external cancellation"
    assert session.current_run_status() == "idle"
    background.shutdown(session.control_plane)
    store.close()


def test_durable_run_persists_history_and_child_side_effect_identity(tmp_path):
    """A background Run remains queryable after its source Session disappears."""
    workspace, store, session, scheduler, _events, background, services = _runtime(tmp_path)
    store.create_automation(
        name="parent-child", prompt="delegate", trigger=TriggerSpec(type="once", run_at=0), now=0
    )
    run_id = store.materialize_due(now=0)[0]
    store.claim_next_run(owner_id=scheduler.host_id, now=0)
    store.start_run(run_id, owner_id=scheduler.host_id, now=0)
    marker = workspace / "child.txt"

    def write_marker(_args, _runtime):
        marker.write_text("child", encoding="utf-8")
        return ToolResult.success({"path": str(marker)})

    class Script:
        context_limit = 128_000

        def __init__(self):
            self.responses = [
                response(calls=[{"id": "same-provider-call", "name": "spawn_agent", "arguments": {"task": "write it"}}]),
                response(calls=[{"id": "same-provider-call", "name": "write_marker", "arguments": {}}]),
                response(content="child done", calls=[]),
                response(content="parent done", calls=[]),
            ]

        def __call__(self, _messages, **_kwargs):
            yield event(self.responses.pop(0))

    llm = Script()
    root_journal = _DurableToolJournal(scheduler, run_id, agent_task_id="root-task")
    tools = build_agent_tools(
        llm,
        [Tool("write_marker", "write marker", {"type": "object"}, write_marker)],
        max_depth=1,
        render_subagents=False,
    )
    root = Agent(
        llm,
        tools,
        session,
        SilentRenderer(),
        services=services,
        execution_journal=root_journal,
        execution_journal_factory=lambda task_id, parent_id: root_journal.child(task_id, parent_id),
        profile=AgentProfile(
            "durable", frozenset(tool.name for tool in tools), max_steps=6,
            allow_delegation=True,
        ),
    )
    assert root.executor.capabilities.delegation is not None
    assert root.executor.capabilities.delegation.execution_journal_factory is not None

    assert root.run("delegate") == "parent done"
    assert marker.read_text(encoding="utf-8") == "child"
    history = store.run_history(run_id)
    event_types = [item["event_type"] for item in history["history"]]
    assert {"user_input", "model_step", "tool_intent", "tool_started", "tool_result"} <= set(event_types)
    executions = store.list_tool_executions(run_id)
    assert any(item["agent_task_id"] == "root-task" for item in executions)
    child_execution = next(item for item in executions if item["tool_name"] == "write_marker")
    assert child_execution["agent_task_id"]
    assert child_execution["parent_agent_task_id"] == "root-task"
    assert child_execution["session_run_id"]
    assert child_execution["step_id"]
    parent_execution = next(item for item in executions if item["tool_name"] == "spawn_agent")
    assert parent_execution["provider_call_id"] == child_execution["provider_call_id"]
    assert parent_execution["execution_call_id"] != child_execution["execution_call_id"]

    background.shutdown(session.control_plane)
    store.finish_run(run_id, status="completed", result="parent done")
    root_journal.record_event(
        "run_finished", {"status": "completed", "result": "parent done"},
        event_key="run-finished",
    )
    store.close()

    reopened = AutonomyStore(
        tmp_path / "tasks.sqlite3",
        session_id="different-source-session",
        workspace_dir=workspace,
    )
    try:
        persisted = reopened.run_history(run_id)
        assert persisted["run"]["status"] == "completed"
        assert persisted["history"][-1]["event_type"] == "run_finished"
    finally:
        reopened.close()
