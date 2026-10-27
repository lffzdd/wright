"""Diagnostics go to a log file; the terminal only shows warnings."""

import logging

from wright.core.logger import get_logger


def test_info_goes_to_the_log_file_not_the_terminal(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path))
    monkeypatch.delenv("WRIGHT_LOG_LEVEL", raising=False)
    logger = get_logger("wright.test.logging")
    terminal = next(h for h in logger.handlers if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler))
    shown = []
    monkeypatch.setattr(terminal, "emit", lambda record: shown.append(record.getMessage()))

    logger.info("routine memory diagnostic")
    logger.warning("something the person should see")
    monkeypatch.setenv("WRIGHT_LOG_LEVEL", "INFO")
    logger.info("shown when troubleshooting")

    assert shown == ["something the person should see", "shown when troubleshooting"]
    written = (tmp_path / "logs" / "wright.log").read_text()
    assert "routine memory diagnostic" in written
