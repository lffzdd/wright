"""Assembly failure releases recorded resources and leaves a shared host alone."""

from __future__ import annotations

from wright.application.composition.runtime import _OpenedRuntime


class _Close:
    def __init__(self, name: str, sink: list[object], *, result: list[str] | None = None) -> None:
        self.name = name
        self.sink = sink
        self.result = result

    def close(self) -> list[str]:
        self.sink.append(self.name)
        return list(self.result or [])


class _Shutdown:
    def __init__(self, name: str, sink: list[object]) -> None:
        self.name = name
        self.sink = sink

    def shutdown(self, plane: object = None) -> None:
        if plane is None:
            self.sink.append(self.name)
        else:
            self.sink.append((self.name, plane))


def test_abort_releases_owned_resources_once_and_spares_a_shared_host() -> None:
    sink: list[object] = []
    session = _Close("session", sink)
    session.control_plane = "plane"  # type: ignore[attr-defined]
    session.mark_commands_cancel_requested = (  # type: ignore[attr-defined]
        lambda tasks, reason: sink.append(("mark", tuple(tasks), reason))
    )
    opened = _OpenedRuntime(None)
    opened.session_state = session  # type: ignore[assignment]
    opened.runtime_resources = _Close("resources", sink, result=["task"])  # type: ignore[assignment]
    opened.interaction_broker = _Close("broker", sink)
    opened.loop_registry = _Close("loops", sink)  # type: ignore[assignment]
    opened.created_host = _Close("created", sink)  # type: ignore[assignment]
    opened.background_runtime = _Shutdown("background", sink)  # type: ignore[assignment]
    opened.autonomy_store = _Close("store", sink)  # type: ignore[assignment]
    opened.mcp_manager = _Shutdown("mcp", sink)  # type: ignore[assignment]
    opened.publisher = _Close("publisher", sink)  # type: ignore[assignment]
    opened.owns_publisher = True

    opened.abort()
    opened.abort()

    assert sink == [
        "resources",
        ("mark", ("task",), "runtime assembly failed"),
        "broker",
        "loops",
        "created",
        ("background", "plane"),
        "store",
        "mcp",
        "publisher",
    ]

    shared = _Close("shared", sink)
    spared = _OpenedRuntime(shared)  # type: ignore[arg-type]
    spared.autonomy_store = _Close("shared-store", sink)  # type: ignore[assignment]
    spared.abort()
    assert "shared" not in sink
    assert "shared-store" not in sink
