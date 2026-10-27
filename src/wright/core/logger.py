"""Process logging for Wright.

The terminal belongs to the person: the CLI and TUI draw their own interface
there, so routine diagnostics (memory recall, selection, extraction) must not
be interleaved with it. Records at INFO and above go to a log file under the
Wright home directory; only warnings and errors reach the terminal.

``WRIGHT_LOG_LEVEL`` sets what reaches the terminal (default ``WARNING``;
``INFO`` or ``DEBUG`` for troubleshooting). Both the variable and the home
directory are read when a record is written, because ``.env`` is loaded after
modules (and their loggers) are imported.
"""

from __future__ import annotations

import logging
import os
import threading
from logging import Formatter, StreamHandler, getLogger
from logging.handlers import RotatingFileHandler

_FORMAT = Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
_MAX_BYTES = 5 * 1024 * 1024


class _TerminalLevel(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        name = os.environ.get("WRIGHT_LOG_LEVEL", "").strip().upper() or "WARNING"
        level = logging.getLevelName(name)
        return record.levelno >= (level if isinstance(level, int) else logging.WARNING)


class _WrightLogFile(logging.Handler):
    """Append to ``<wright home>/logs/wright.log``, opened on first use.

    A log file that cannot be opened must never break the program: the
    handler then stays silent and the terminal still gets warnings.
    """

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self._lock = threading.Lock()
        self._target: RotatingFileHandler | None = None

    def emit(self, record: logging.LogRecord) -> None:
        with self._lock:
            try:
                from .paths import wright_home

                path = wright_home() / "logs" / "wright.log"
                # WRIGHT_HOME can change after the first record (``.env``,
                # tests); follow it instead of writing to a stale place.
                if self._target is None or self._target.baseFilename != str(path.resolve()):
                    if self._target is not None:
                        self._target.close()
                    path.parent.mkdir(parents=True, exist_ok=True)
                    self._target = RotatingFileHandler(
                        path, maxBytes=_MAX_BYTES, backupCount=3, encoding="utf-8",
                    )
                    self._target.setFormatter(_FORMAT)
            except Exception:
                return
            target = self._target
        try:
            target.emit(record)
        except Exception:
            self.handleError(record)


_TERMINAL = StreamHandler()
_TERMINAL.setFormatter(_FORMAT)
_TERMINAL.addFilter(_TerminalLevel())
_FILE = _WrightLogFile()


def get_logger(name: str) -> logging.Logger:
    logger = getLogger(name)
    if not logger.handlers:
        logger.addHandler(_TERMINAL)
        logger.addHandler(_FILE)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def log_file_path() -> str:
    """Where diagnostics are written, for messages that point people to it."""
    from .paths import wright_home

    return str(wright_home() / "logs" / "wright.log")
