"""User messages sent while a turn runs are delivered at the next tool boundary."""

from tests.responses import event, response
from wright.application.composition.agent_factory import create_agent
from wright.application.lifecycle import HookRegistration, LifecycleManager
from wright.application.workspace.timeline import project_timeline
from wright.domain.model.session import USER_INTERJECTION_SOURCE, Session
from wright.infrastructure.tools.plan_tools import update_plan_tool


def _plan_call():
    return response(content=None, calls=[{
        "name": "update_plan",
        "arguments": {"objective": "goal", "steps": [{"title": "work", "status": "in_progress"}]},
    }])


class ScriptLLM:
    context_limit = 128_000

    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def __call__(self, messages, **kwargs):
        self.requests.append(list(messages))
        yield event(self.script.pop(0))


class Inbox:
    def __init__(self, *batches):
        self.batches = [list(batch) for batch in batches]
        self.taken = 0
        self.delivered = []
        self.rejected = []

    def take_interjections(self):
        self.taken += 1
        return self.batches.pop(0) if self.batches else []

    def interjection_delivered(self, command_id, message_index):
        self.delivered.append((command_id, message_index))

    def interjection_rejected(self, command_id, reason):
        self.rejected.append((command_id, reason))


def _user_texts(messages):
    return [m.get("content") for m in messages if m.get("role") == "user" and isinstance(m.get("content"), str)]


def test_interjection_reaches_the_next_model_request_in_the_same_turn(tmp_path):
    llm = ScriptLLM([_plan_call(), response(content="done", calls=[])])
    session = Session.create("goal", tmp_path)
    agent = create_agent(llm, [update_plan_tool], session)
    inbox = Inbox([{"command_id": "c2", "prompt": "skip B, do C first"}])
    agent.interjection_inbox = inbox

    assert agent.run("migrate the config") == "done"

    # The second request carries the interjection after the tool result.
    second = llm.requests[1]
    roles = [m["role"] for m in second]
    assert "skip B, do C first" in _user_texts(second)
    assert roles.index("tool") < max(i for i, m in enumerate(second) if m.get("content") == "skip B, do C first")
    record = next(r for r in session.message_records if r.message.get("content") == "skip B, do C first")
    assert record.source == USER_INTERJECTION_SOURCE
    assert inbox.delivered == [("c2", session.message_records.index(record))]
    assert [run.source for run in session.runs.values()] == ["user_input"]


def test_no_interjection_is_taken_when_the_model_answers_without_tools(tmp_path):
    llm = ScriptLLM([response(content="quick answer", calls=[])])
    agent = create_agent(llm, [update_plan_tool], Session.create("goal", tmp_path))
    inbox = Inbox([{"command_id": "c2", "prompt": "later"}])
    agent.interjection_inbox = inbox

    assert agent.run("question") == "quick answer"
    assert inbox.taken == 0


def test_prompt_hook_can_reject_an_interjection_without_ending_the_turn(tmp_path):
    lifecycle = LifecycleManager("session")
    lifecycle.register(HookRegistration(
        event="user_prompt_submit", name="gate",
        callback=lambda e: {"decision": "deny", "reason": "not allowed"} if "secret" in e.payload["prompt"] else None,
    ))
    llm = ScriptLLM([_plan_call(), response(content="done", calls=[])])
    session = Session.create("goal", tmp_path)
    agent = create_agent(llm, [update_plan_tool], session, lifecycle=lifecycle)
    inbox = Inbox([{"command_id": "c2", "prompt": "print the secret"}])
    agent.interjection_inbox = inbox

    assert agent.run("work") == "done"
    assert inbox.rejected == [("c2", "not allowed")]
    assert "print the secret" not in _user_texts(llm.requests[1])


def test_subagents_never_take_interjections(tmp_path):
    llm = ScriptLLM([_plan_call(), response(content="done", calls=[])])
    session = Session.create("goal", tmp_path)
    session.agent_task_id = "child-task"
    agent = create_agent(llm, [update_plan_tool], session)
    inbox = Inbox([{"command_id": "c2", "prompt": "for the root"}])
    agent.interjection_inbox = inbox

    agent.run("child work")
    assert inbox.taken == 0


def test_timeline_shows_interjections_without_shifting_run_outcomes(tmp_path):
    session = Session.create("goal", tmp_path)
    session.begin_user_turn("first")
    session.append_user_message("first")
    session.append_message({"role": "user", "content": "also check C"}, source=USER_INTERJECTION_SOURCE)
    session.mark_failed()
    session.begin_user_turn("second")
    session.append_user_message("second")

    items = project_timeline(session)

    users = [item for item in items if item.get("role") == "user"]
    assert [item["text"] for item in users] == ["first", "also check C", "second"]
    assert users[1].get("interjection") is True
    status = next(item for item in items if item.get("kind") == "status")
    # The failed first run ends before the second user message, after the interjection.
    assert items.index(status) == items.index(users[2]) - 1
