"""Stop takes effect while the model is still streaming."""

import threading

from wright.application.composition.agent_factory import create_agent
from wright.domain.model.llm.events import ContentDelta, ContentDone
from wright.domain.model.session import Session


class SlowLLM:
    context_limit = 128_000

    def __init__(self, stop: threading.Event):
        self.stop = stop
        self.pieces_sent = 0
        self.closed = False

    def __call__(self, request, **kwargs):
        try:
            for index in range(1_000):
                if index == 3:
                    self.stop.set()  # the person presses Stop mid-answer
                self.pieces_sent += 1
                yield ContentDelta(f"word{index} ")
            yield ContentDone("never", finish_reason="stop")
        finally:
            self.closed = True


def test_stop_ends_the_stream_and_keeps_the_visible_text(tmp_path):
    stop = threading.Event()
    llm = SlowLLM(stop)
    session = Session.create("goal", tmp_path)
    agent = create_agent(llm, [], session)

    assert agent.run("talk", cancellation_check=stop.is_set) is None

    assert llm.pieces_sent < 10
    assert llm.closed
    assert session.current_run_status() == "cancelled"
    assistant = [m for m in session.messages if m.get("role") == "assistant"]
    assert assistant and assistant[-1]["content"].startswith("word0 word1 word2")
