"""The task engine behind the web UI and the CLI.

Threads:
  download thread   - works through the queue one task at a time (yt-dlp, network bound)
  processing thread - remuxes, renames and writes NFOs for finished videos (disk bound)
  scheduler         - optional: queues "Update all" every N hours (off unless enabled in settings)
so a playlist keeps downloading while earlier videos are being processed.
"""
from __future__ import annotations

import itertools
import logging
import threading
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from yt_dlp.utils import DownloadCancelled

from . import config, kodi
from .library import Show
from .logs import DETAIL
from .sync import Report, Task, run_task, submit_finalize, tasks_for_all
from .util import format_bytes, format_duration, is_disk_full, read_json, write_json
from .ytdl import parse_rate

log = logging.getLogger(__name__)


@dataclass
class _Job:
    label: str
    show: Show
    fn: Callable[[], None]
    report: Report
    video_id: str | None = None
    final: bool = False  # last job of a task: records the task in the history afterwards


class TaskContext:
    """Handed to sync.run_task / ytdl: progress reporting, cancellation and job submission."""

    def __init__(self, engine: Engine, task: Task):
        self._engine = engine
        self.task = task
        task.report = task.report or Report(kind=task.kind, name=task.label)
        self.report = task.report
        self.show: Show | None = None
        self._last_log = 0.0
        self._logged_title = ""

    def check_cancelled(self) -> None:
        if self._engine._cancel.is_set():
            raise DownloadCancelled("Cancelled by user")

    def set_phase(self, text: str, done: int | None = None, total: int | None = None) -> None:
        self._engine._update_progress({"phase": text, "done": done, "total": total})

    def on_progress(self, d: dict) -> None:
        self.check_cancelled()
        info = d.get("info_dict") or {}
        if not info.get("title"):
            return  # subtitle/thumbnail transfers
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            part = "audio" if info.get("vcodec") == "none" else "video"
            self._engine._update_progress({
                "phase": "Downloading",
                "video": info["title"],
                "done": info.get("playlist_autonumber"),
                "total": info.get("n_entries"),
                "percent": round(done / total * 100, 1) if total else None,
                "detail": f"{part} stream: {format_bytes(done)} of {format_bytes(total)}" if total else part,
                "speed": f"{format_bytes(d.get('speed'))}/s" if d.get("speed") else "",
                "eta": format_duration(d.get("eta")) if d.get("eta") is not None else "",
            })
            now = time.monotonic()
            if info["title"] != self._logged_title:  # first progress report of a new video
                self._logged_title, self._last_log = info["title"], now
                log.log(DETAIL, "Started downloading %s", info["title"])
            elif now - self._last_log > 30 and total:
                self._last_log = now
                log.log(DETAIL, "Still downloading %s: %d%% done", info["title"], done / total * 100)
        elif d["status"] == "finished":
            self._engine._update_progress({"phase": "Merging", "video": info["title"], "percent": 100.0,
                                           "speed": "", "eta": ""})

    def attach_downloader(self, ydl) -> None:
        self._engine._ydl = ydl

    def ratelimit(self) -> int | None:
        return parse_rate(self._engine.speed_limit)

    def warning(self, message: str) -> None:
        pass  # yt-dlp warnings are logged by ytdl._Logger; they are too noisy for the activity list

    def error(self, message: str) -> None:
        self.report.error(message)
        if is_disk_full(message):
            self._engine._disk_full()

    def show_lock(self, name: str) -> threading.Lock:
        return self._engine.show_lock(name)

    def in_flight(self, show_name: str) -> set[str]:
        return self._engine._in_flight_ids(show_name)

    def submit(self, label: str, show: Show, fn: Callable[[], None], video_id: str | None = None,
               final: bool = False) -> None:
        self._engine._submit_job(_Job(label, show, fn, self.report, video_id, final))


class Engine:
    def __init__(self, autostart: bool = True, scheduler: bool = False):
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._ids = itertools.count(1)
        self._pending: list[Task] = []
        self._current: Task | None = None
        self._paused = False
        self._pause_reason = ""
        self._stopping = False
        self._cancel = threading.Event()
        self._progress: dict = {}
        self._ydl = None
        self.speed_limit: str | None = config.settings.speed_limit or None
        self.history: deque[Report] = deque(maxlen=30)
        self.library_version = 0

        self._jobs: deque[_Job] = deque()
        self._job_label: str | None = None
        self._in_flight: dict[str, Counter] = defaultdict(Counter)
        self._show_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._library_changed = False
        if autostart:
            self.start(scheduler)

    def start(self, scheduler: bool = False) -> None:
        threading.Thread(target=self._download_loop, name="download", daemon=True).start()
        threading.Thread(target=self._processing_loop, name="processing", daemon=True).start()
        if scheduler:
            threading.Thread(target=self._schedule_loop, name="scheduler", daemon=True).start()

    # ------------------------------------------------------------------ queue control

    def enqueue(self, task: Task) -> bool:
        """Adds a task unless an identical one is already queued or running."""
        with self._lock:
            key = (task.kind, task.name or task.url)
            if any((t.kind, t.name or t.url) == key for t in self._pending + [self._current] if t):
                return False
            task.id = next(self._ids)
            task.report = Report(kind=task.kind, name=task.label)
            self._pending.append(task)
            self._changed.notify_all()
            return True

    def enqueue_all(self, kind: str = "update") -> int:
        """Update all (subscribed shows) or Repair all (everything in the library); see tasks_for_all."""
        return sum(self.enqueue(task) for task in tasks_for_all(kind))

    def remove(self, task_id: int) -> bool:
        with self._lock:
            before = len(self._pending)
            self._pending = [t for t in self._pending if t.id != task_id]
            return len(self._pending) != before

    def pause(self) -> None:
        with self._lock:
            self._paused, self._pause_reason = True, ""
            if self._current:
                self._cancel.set()

    def resume(self) -> None:
        with self._lock:
            self._paused, self._pause_reason = False, ""
            self._changed.notify_all()

    def stop(self) -> None:
        """Clears the queue and cancels the running download. Finished videos are still processed."""
        with self._lock:
            self._pending.clear()
            self._paused, self._pause_reason = False, ""
            if self._current:
                self._stopping = True
                self._cancel.set()
            self._changed.notify_all()

    def set_speed_limit(self, value: str | None) -> None:
        rate = parse_rate(value)  # raises ValueError for nonsense such as "fast"
        if value and rate is None:
            raise ValueError(f"Invalid speed limit: {value}")
        with self._lock:
            self.speed_limit = value or None
            if self._ydl is not None:
                self._ydl.params["ratelimit"] = rate  # yt-dlp reads this per chunk, so it applies immediately

    def apply_settings(self) -> None:
        """Picks up changed settings (called after the Settings page saved them)."""
        self.set_speed_limit(config.settings.speed_limit or None)

    def next_auto_update(self) -> datetime | None:
        hours = config.settings.auto_update_hours
        if not hours:
            return None
        last = (read_json(config.STATE_FILE) or {}).get("last_auto_update", 0)
        return datetime.fromtimestamp(max(last + hours * 3600, time.time()))

    def show_lock(self, name: str) -> threading.Lock:
        with self._lock:
            return self._show_locks[name]

    def wait_until_idle(self, timeout: float | None = None) -> bool:
        """Blocks until nothing is running or processing and the queue is empty (or paused).
        Returns False on timeout."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            while self._current or (self._pending and not self._paused) or self._jobs or self._job_label:
                if deadline is not None and time.monotonic() > deadline:
                    return False
                self._changed.wait(1)
        return True

    # ------------------------------------------------------------------ status

    def status(self) -> dict:
        with self._lock:
            current = None
            if self._current:
                current = {"id": self._current.id, "kind": self._current.kind, "name": self._current.label,
                           **self._progress}
            if self._paused:
                state = "paused"
            elif self._current or self._jobs or self._job_label:
                state = "running"
            else:
                state = "idle"
            return {
                "state": state,
                "pause_reason": self._pause_reason if self._paused else "",
                "current": current,
                "queue": [{"id": t.id, "kind": t.kind, "name": t.label} for t in self._pending],
                "processing": {"current": self._job_label, "pending": len(self._jobs)},
                "history": [r.as_dict() for r in self.history],
                "speed_limit": self.speed_limit,
                "library_version": self.library_version,
                "next_auto_update": (lambda n: n.strftime("%a %H:%M") if n else None)(self.next_auto_update()),
            }

    def _update_progress(self, values: dict) -> None:
        with self._lock:
            if values.get("phase") != "Downloading" and "video" not in values:
                self._progress = {}
            self._progress.update(values)

    # ------------------------------------------------------------------ download thread

    def _download_loop(self) -> None:
        while True:
            with self._lock:
                while self._paused or not self._pending:
                    self._changed.wait()
                task = self._pending.pop(0)
                self._current, self._progress = task, {}
                self._cancel.clear()
                self._stopping = False
            ctx = TaskContext(self, task)
            started = time.monotonic()
            log.debug("Task #%d started: %s %s", task.id, task.kind, task.label)
            try:
                run_task(task, ctx)
            except DownloadCancelled:
                with self._lock:
                    paused = self._paused and not self._stopping
                    if paused:
                        self._pending.insert(0, task)  # resumes where it left off (.part files are kept)
                if not paused:
                    task.report.status = "cancelled"
                    task.report.note = "Stopped."
                    self._finish(ctx)
            except Exception as e:  # noqa: BLE001 - a failed task must not kill the thread
                if is_disk_full(e):
                    self._disk_full()
                    with self._lock:
                        self._pending.insert(0, task)  # runs again after Resume
                else:
                    log.exception("Task failed: %s", task.label)
                    task.report.status = "error"
                    task.report.errors.append(str(e))
                    task.report.note = "Failed."
                    self._finish(ctx)
            finally:
                log.debug("Task #%d left the download thread after %.0fs", task.id, time.monotonic() - started)
                with self._lock:
                    self._current, self._progress = None, {}
                    self._changed.notify_all()

    def _finish(self, ctx: TaskContext) -> None:
        """Organizes whatever was downloaded before a task stopped/failed, then records it."""
        if ctx.show:
            submit_finalize(ctx, ctx.show)
        else:
            self._record(ctx.report)

    def _disk_full(self) -> None:
        """Pauses the queue when the disk is full, instead of letting every following task fail."""
        with self._lock:
            if self._paused:
                return
            self._paused, self._pause_reason = True, "The disk is full. Free up some space, then press Resume."
            if self._current:
                self._cancel.set()
        log.error("The disk is full, so downloading is paused. Free up some space, then press Resume "
                  "(or run the command again).")

    def _record(self, report: Report) -> None:
        report.finished = datetime.now()
        with self._lock:
            self.history.appendleft(report)
        log.info("%s %s: %s", report.kind.capitalize(), report.name, report.summary())
        for err in report.errors:
            log.info("  - %s", err)

    # ------------------------------------------------------------------ processing thread

    def _submit_job(self, job: _Job) -> None:
        with self._lock:
            if job.video_id:
                self._in_flight[job.show.name][job.video_id] += 1
            self._jobs.append(job)
            self._changed.notify_all()

    def _in_flight_ids(self, show_name: str) -> set[str]:
        with self._lock:
            return {vid for vid, n in self._in_flight[show_name].items() if n > 0}

    def _processing_loop(self) -> None:
        while True:
            with self._lock:
                while not self._jobs:
                    self._changed.wait()
                job = self._jobs.popleft()
                self._job_label = job.label
            started, errors_before = time.monotonic(), len(job.report.errors)
            try:
                with self.show_lock(job.show.name):
                    job.fn()
                self._library_changed = True
            except Exception as e:  # noqa: BLE001
                if is_disk_full(e):
                    log.error("%s: %s", job.label, e)
                else:
                    log.exception("Processing failed: %s", job.label)
                job.report.error(f"{job.label}: {e}")
            finally:
                log.debug("%s took %.1fs", job.label, time.monotonic() - started)
                if any(is_disk_full(err) for err in job.report.errors[errors_before:]):
                    self._disk_full()
                with self._lock:
                    if job.video_id:
                        self._in_flight[job.show.name][job.video_id] -= 1
                    self._job_label = None
                    self.library_version += 1
                    idle = not (self._jobs or self._current or self._pending)
                    self._changed.notify_all()
            if job.final:
                self._record(job.report)
            if idle and self._library_changed:
                self._library_changed = False
                kodi.scan_library()

    # ------------------------------------------------------------------ scheduler

    def _schedule_loop(self) -> None:
        """Queues "Update all" when the configured interval has passed (checked once a minute)."""
        while True:
            time.sleep(60)
            due = self.next_auto_update()
            if due and due <= datetime.now() and self.status()["state"] == "idle":
                state = read_json(config.STATE_FILE) or {}
                state["last_auto_update"] = time.time()
                write_json(config.STATE_FILE, state, indent=2)
                log.info("Automatic update: queued %d subscription(s)", self.enqueue_all("update"))
