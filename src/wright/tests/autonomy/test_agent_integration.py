import json
import queue
import time

from ...agent import Agent
from ...agent_background import AgentBackgroundRuntime
from ...autonomy import AutonomyScheduler, AutonomyStore, TriggerSpec
from ...autonomy.runner import launch_durable_run
from ...events import ContentDone
from ...permission import PermissionSettings
from ...renderer import SilentRenderer
from ...session import SessionState
from ...tools.ask_user_tool import ask_user_tool
from ...tools.autonomy_tools import autonomy_tools
from ...tools.knowledge_tools import build_knowledge_tools
from ...tools.memory_tools import build_memory_tools
from ...tools.skill_tools import build_skill_tools
from ...skills.registry import SkillRegistry
from ...skills.store import write_skill


def _final(answer):
    return json.dumps({"tool_calls": [], "final_answer": answer})


class ScriptLLM:
    context_limit = 128_000

    def __init__(self, answers):
        self.answers = list(answers)

    def __call__(self, messages):
        yield ContentDone(_final(self.answers.pop(0)))


class SlowLLM:
    context_limit = 128_000

    def __init__(self, answer, delay=0.2):
        self.answer = answer
        self.delay = delay

    def __call__(self, messages):
        time.sleep(self.delay)
        yield ContentDone(_final(self.answer))


def _runtime(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = AutonomyStore(
        tmp_path / "tasks.sqlite3",
        session_id="session",
        workspace_dir=workspace,
    )
    events = queue.Queue()
    session = SessionState.create("interactive", workspace)
    session.session_id = "session"
    session.durable_task_store = store
    background = AgentBackgroundRuntime(events, max_workers=1)
    session.agent_background_runtime = background
    scheduler = AutonomyScheduler(store, events, poll_interval=1)
    return workspace, store, session, scheduler, events, background


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
    workspace, store, session, scheduler, events, background = _runtime(tmp_path)
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
    root_goal = session.user_goal
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
    )
    assert launch is not None
    event_type, finished_id = events.get(timeout=2)
    assert event_type == "DURABLE_RUN_FINISHED"
    assert finished_id == run_id

    assert session.user_goal == root_goal
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
    workspace, store, session, scheduler, events, background = _runtime(tmp_path)
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
    assert session.user_goal == "please keep chatting"

    event_type, finished_id = events.get(timeout=2)
    assert event_type == "DURABLE_RUN_FINISHED"
    assert finished_id == run_id
    assert store.get_run(run_id).status == "completed"
    background.shutdown(session.control_plane)
    store.close()


def test_durable_session_omits_ask_user_and_autonomy_tools(tmp_path):
    workspace, store, session, scheduler, events, background = _runtime(tmp_path)
    run_id = _claim_run(store)
    memory_tools = build_memory_tools(
        tmp_path / "memory", include_legacy_save=True
    )
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
    )
    assert launch is not None
    names = set(launch.tool_names)
    assert "ask_user" not in names
    assert "create_task" not in names
    assert "pause_schedule" not in names
    assert "resume_schedule" not in names
    assert "cancel_schedule" not in names
    assert "get_schedule" not in names
    assert "list_schedules" not in names
    assert "list_task_runs" not in names
    assert "create_memory" not in names
    assert "search_memory" not in names
    assert "save_memory" not in names
    assert "knowledge_search" not in names
    assert "skill" not in names
    assert "list_skills" not in names
    assert "load_skill" not in names
    assert "unload_skill" not in names
    events.get(timeout=2)
    background.shutdown(session.control_plane)
    store.close()


def test_cancelled_dispatched_run_is_not_started(tmp_path):
    workspace, store, session, scheduler, events, background = _runtime(tmp_path)
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
    )
    assert launch is None
    run = store.get_run(run_id)
    assert run.status == "cancelled"
    assert run.cancel_reason == "external cancellation"
    assert session.status == "running"
    background.shutdown(session.control_plane)
    store.close()
