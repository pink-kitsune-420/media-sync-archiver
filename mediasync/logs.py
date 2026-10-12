"""Logging: the important lines on the console, everything in a log file.

Console   tasks starting and finishing, new videos, problems; errors as one line each.
Log file  logs/mediasync-YYYY-MM-DD.log in the settings folder, one file per day (the newest
          KEEP_DAYS are kept): the console lines plus file renames, download progress, yt-dlp's own
          warnings and full error tracebacks. With "Detailed log" on (settings: detailed_log) it
          also records every request yt-dlp makes and why videos are skipped, fixed or downloaded.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import date
from pathlib import Path

from . import __version__, config

# Between DEBUG and INFO: always written to the log file, never shown on the console.
DETAIL = 15
logging.addLevelName(DETAIL, "DETAIL")

KEEP_DAYS = 14
YTDLP_LOGGER = "mediasync.yt_dlp"  # yt-dlp's own messages: log file only (problems reach the task report)

_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"
_handlers: list[logging.Handler] = []


class _DailyFile(logging.FileHandler):
    """logs/mediasync-YYYY-MM-DD.log, continuing in a new file after midnight."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.day = date.today()
        folder.mkdir(parents=True, exist_ok=True)
        super().__init__(self._path(), encoding="utf-8", delay=True)
        self._prune()

    def _path(self) -> str:
        return os.path.abspath(self.folder / f"mediasync-{self.day.isoformat()}.log")

    def emit(self, record: logging.LogRecord) -> None:
        if date.today() != self.day:
            self.day = date.today()
            if self.stream:
                self.stream.close()
                self.stream = None  # reopened under the new name by FileHandler.emit
            self.baseFilename = self._path()
            self._prune()
        super().emit(record)

    def _prune(self) -> None:
        for old in sorted(self.folder.glob("mediasync-*.log"))[:-KEEP_DAYS]:
            old.unlink(missing_ok=True)


class _ConsoleFormatter(logging.Formatter):
    """One line per message: tracebacks only go to the log file."""

    def format(self, record: logging.LogRecord) -> str:
        if record.exc_info:
            exc = record.exc_info[1]
            text = f"{record.getMessage()}: {exc or type(exc).__name__}"
            if any(isinstance(h, _DailyFile) for h in _handlers):
                text += " (details in the log file)"
            record = logging.makeLogRecord({**record.__dict__, "msg": text, "args": (),
                                            "exc_info": None, "exc_text": None})
        return super().format(record)


def _for_console(record: logging.LogRecord) -> bool:
    return record.levelno >= logging.INFO and not record.name.startswith(YTDLP_LOGGER)


def setup(command: str) -> Path | None:
    """Installs the console and log file handlers. Returns the log folder (None if it is not writable)."""
    root = logging.getLogger()
    for handler in _handlers:
        root.removeHandler(handler)
        handler.close()
    _handlers.clear()

    console = logging.StreamHandler(sys.stderr)
    console.addFilter(_for_console)
    console.setFormatter(_ConsoleFormatter(_FORMAT, "%H:%M:%S"))
    _handlers.append(console)
    try:
        log_file = _DailyFile(config.LOG_DIR)
    except OSError as e:
        log_file = None
        print(f"Logging to the console only: cannot write to {config.LOG_DIR} ({e})", file=sys.stderr)
    else:
        log_file.setFormatter(logging.Formatter(_FORMAT, "%Y-%m-%d %H:%M:%S"))
        _handlers.append(log_file)

    for handler in _handlers:
        root.addHandler(handler)
    root.setLevel(logging.WARNING)  # other libraries: warnings only
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    set_detailed(config.settings.detailed_log)
    logging.getLogger(__name__).log(DETAIL, "Media Sync Archiver %s started (%s)", __version__, command)
    return config.LOG_DIR if log_file else None


def set_detailed(on: bool) -> None:
    """Detailed log on/off; takes effect immediately."""
    logging.getLogger("mediasync").setLevel(logging.DEBUG if on else DETAIL)
