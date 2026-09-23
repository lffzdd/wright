from __future__ import annotations

from wright.engine.cancellation import CancellationToken


def test_cancellation_token_combines_host_and_run_checks_and_restores_binding():
    host_cancelled = False
    run_cancelled = False
    token = CancellationToken(lambda: host_cancelled)

    assert not token.is_cancelled()
    with token.bind_run(lambda: run_cancelled):
        assert not token.is_cancelled()
        run_cancelled = True
        assert token.is_cancelled()

    run_cancelled = False
    assert not token.is_cancelled()
    host_cancelled = True
    assert token.is_cancelled()
