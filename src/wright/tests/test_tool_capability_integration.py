import queue
import threading

from wright.autonomy import AutonomyScheduler, AutonomyStore
from wright.executor import ToolExecutor
from wright.looping import SessionLoopRegistry
from wright.permission import PermissionCheckResult, PermissionResolver
from wright.services import RuntimeServices
from wright.session import Session
from wright.tools.autonomy_tools import autonomy_tools
from wright.tools.base import ToolCall
from wright.tools.loop_tools import manage_loop_tool
from wright.tools.task_tools import task_tools


def test_registered_management_tools_work_through_capability_restriction(tmp_path):
    store = AutonomyStore(tmp_path / "tasks.db", session_id="session", workspace_dir=tmp_path)
    events = queue.Queue()
    idle = threading.Event()
    loops = SessionLoopRegistry(events, idle)
    scheduler = AutonomyScheduler(store, events)
    services = RuntimeServices(durable_store=store, autonomy_scheduler=scheduler, loop_registry=loops)
    session = Session.create("management", tmp_path, session_id="session")
    session.begin_user_turn("management")
    executor = ToolExecutor(
        {t.name: t for t in [*autonomy_tools, *task_tools, manage_loop_tool]},
        session=session, services=services,
        permission_resolver=PermissionResolver(
            approval_handler=lambda _: PermissionCheckResult("allow", "test authorization"),
        ),
    )

    def call(tool_name, **args):
        result = executor.execute([ToolCall(tool_name, args)])[0].result
        assert result.ok, f"{tool_name}: {result.err}"
        return result.data

    try:
        schedule = call("schedule_task", name="once", prompt="work", trigger={"type": "once", "run_at": 0})
        assert scheduler._wake.is_set()
        assert call("get_schedule", schedule_id=schedule["id"])["id"] == schedule["id"]
        assert call("list_schedules")["count"] == 1
        assert call("pause_schedule", schedule_id=schedule["id"])["status"] == "paused"
        assert call("resume_schedule", schedule_id=schedule["id"])["status"] == "active"
        run_id = store.materialize_due()[0]
        assert call("list_task_runs", schedule_id=schedule["id"])["count"] == 1
        assert call("get_task", task_id=run_id)["id"] == run_id
        assert call("wait_task", task_id=run_id, timeout=0)["wait_timed_out"]
        assert call("list_tasks", include_all_turns=True)["count"] == 1
        assert call("cancel_task", task_id=run_id)["status"] == "cancelled"
        assert call("cancel_schedule", schedule_id=schedule["id"])["status"] == "cancelled"
        loop = call("manage_loop", action="create", interval_seconds=60, prompt="work")
        assert call("manage_loop", action="list")["count"] == 1
        call("manage_loop", action="stop", loop_id=loop["id"])
    finally:
        loops.close()
        scheduler.close()
        store.close()
