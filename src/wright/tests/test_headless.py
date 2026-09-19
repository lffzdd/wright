from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

from .. import headless
from ..runtime import RuntimeConfig


def test_headless_entry_starts_and_stops_the_application_host(monkeypatch, tmp_path, capsys):
    calls: list[object] = []

    class Host:
        def start(self):
            calls.append("start")

    runtime = SimpleNamespace(
        application_host=Host(),
        project_context=SimpleNamespace(execution_root=Path(tmp_path)),
    )
    monkeypatch.setattr(
        headless, "assemble_runtime",
        lambda config, **kwargs: calls.append(kwargs) or runtime,
    )
    monkeypatch.setattr(headless, "shutdown_runtime", lambda value: calls.append(value))
    stop = threading.Event()
    stop.set()

    headless.run_headless_host(
        RuntimeConfig(workspace=Path(tmp_path)), source_session_id="origin", stop_event=stop
    )

    assert calls[0] == {"start_automation": False, "automation_session_id": "origin"}
    assert calls[1:] == ["start", runtime]
    assert str(tmp_path) in capsys.readouterr().out
