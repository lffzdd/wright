from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

from wright.application.composition.runtime import RuntimeConfig
from wright.interfaces import headless


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

    kwargs = calls[0]
    assert kwargs["start_automation"] is False
    assert kwargs["automation_session_id"] == "origin"
    assert kwargs["prompter"].__class__.__name__ == "DeniedPrompter"
    assert "renderer" not in kwargs
    assert calls[1:] == ["start", runtime]
    assert str(tmp_path) in capsys.readouterr().out
