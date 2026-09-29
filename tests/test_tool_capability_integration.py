import queue
import threading

from wright.application.scheduling.scheduler import JobScheduler
from wright.application.composition.services import RuntimeServices
from wright.application.session.loops import SessionLoopRegistry
from wright.application.tool_execution.capabilities import assemble_tool_capabilities
from wright.application.tool_execution.dispatch import ToolDispatchService
from wright.domain.model.session import Session
from wright.domain.model.tool import ToolCall
from wright.domain.policy import PermissionResolver, PermissionResponse
from wright.infrastructure.persistence.autonomy_store import AutonomyStore
from wright.infrastructure.tools.schedule import schedule_tools
from wright.infrastructure.tools.loop_tools import manage_loop_tool


def test_registered_management_tools_work_through_capability_restriction(tmp_path):
    store = AutonomyStore(tmp_path / "tasks.db", session_id="session", workspace_dir=tmp_path)
    events = queue.Queue()
    idle = threading.Event()
    loops = SessionLoopRegistry(events, idle)
    scheduler = JobScheduler(store, events)
    services = RuntimeServices(durable_store=store, job_scheduler=scheduler, loop_registry=loops)
    session = Session.create("management", tmp_path, session_id="session")
    session.begin_user_turn("management")
    executor = ToolDispatchService(
        {t.name: t for t in [*schedule_tools, manage_loop_tool]},
        assemble_tool_capabilities(session, services, None, expose_scheduling=True),
        session=session,
        permission_resolver=PermissionResolver(
            approval_handler=lambda _: PermissionResponse("allow_once"),
        ),
    )

    def call(tool_name, **args):
        result = executor.execute([ToolCall(tool_name, args)])[0].result
        assert result.ok, f"{tool_name}: {result.err}"
        return result.data

    try:
        schedule = call(
            "create_schedule",
            name="once",
            prompt="work",
            trigger={"type": "once", "run_at": 0},
        )
        schedule_id = schedule["schedule_id"]
        assert "id" not in schedule
        assert scheduler._wake.is_set()
        assert call("get_schedule", schedule_id=schedule_id)["schedule_id"] == schedule_id
        assert call("list_schedules")["count"] == 1
        assert call("pause_schedule", schedule_id=schedule_id)["status"] == "paused"
        assert call("resume_schedule", schedule_id=schedule_id)["status"] == "active"
        run_id = store.materialize_due()[0]
        listed = call("list_schedule_runs", schedule_id=schedule_id)
        assert listed["count"] == 1
        assert listed["runs"][0]["run_id"] == run_id
        assert listed["runs"][0]["status"] == "queued"
        assert listed["runs"][0]["status"] != "pending"
        observed = call("get_schedule_run", run_id=run_id)
        assert observed["status"] == "queued"
        assert observed["outcome"] == "waiting"
        assert observed["terminal"] is False
        waited = call("wait_schedule_run", run_id=run_id, timeout=0)
        assert waited["wait_timed_out"] is True
        assert waited["status"] == "queued"
        assert store.get_run(run_id).status == "queued"
        cancelled = call("cancel_schedule_run", run_id=run_id)
        assert cancelled["status"] == "cancelled"
        assert cancelled["schedule_changed"] is False
        assert call("cancel_schedule", schedule_id=schedule_id)["status"] == "cancelled"
        loop = call("manage_loop", action="create", interval_seconds=60, prompt="work")
        assert call("manage_loop", action="list")["count"] == 1
        call("manage_loop", action="stop", loop_id=loop["id"])
    finally:
        loops.close()
        scheduler.close()
        store.close()
