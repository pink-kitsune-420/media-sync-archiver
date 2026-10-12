"""Update, Repair and Restore: the steps that bring one show/movie in line with its YouTube source.

Update   Downloads videos the archive has never had, and for videos in the library adds whatever is
         missing: metadata, subtitles, chapters, embedded tags, artwork and .nfo files. Files that
         cannot be read, or lack a video/audio track, are downloaded again. Videos you deleted stay
         deleted ("download once").
Repair   Everything Update does, plus a full read of every file to catch truncated or corrupt
         downloads (downloaded again), a full listing of the source, and rebuilt artwork.
Restore  Downloads the videos you deleted from the library again.

Downloading happens on the download thread; everything that touches the library (remuxing,
renaming, NFOs) runs as jobs on the processing thread, so the next video downloads meanwhile.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from . import config, library, media
from .library import LibraryVideo, Show
from .metadata import ShowState, VideoInfo
from .subscriptions import Subscription, subscriptions
from .util import move_file, read_json, remove_empty_dirs, video_id_from_name

log = logging.getLogger(__name__)

KINDS = ("update", "repair", "restore")
_IMAGE_EXTS = (".jpg", ".jpeg", ".webp", ".png")


@dataclass
class Task:
    kind: str                       # "update", "repair" or "restore"
    url: str | None = None
    name: str | None = None
    mode: str = "playlist"
    limit: int | None = None
    refresh_metadata: bool = False  # re-fetch every video's metadata from YouTube
    id: int = 0
    report: Report | None = field(default=None, repr=False, compare=False)

    @property
    def deep(self) -> bool:
        return self.kind == "repair"

    @property
    def label(self) -> str:
        return self.name or self.url or "?"


@dataclass
class Report:
    """Outcome of one task, filled in by both threads and shown in the web UI's activity list."""
    kind: str
    name: str
    downloaded: int = 0
    redownloaded: int = 0
    fixed: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    status: str = "ok"              # ok | warning | error | cancelled
    note: str = ""
    finished: datetime | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def error(self, message: str) -> None:
        with self._lock:
            if len(self.errors) < 20:
                self.errors.append(message)
            if self.status == "ok":
                self.status = "warning"

    def count_fix(self, what: str, n: int = 1) -> None:
        if n:
            with self._lock:
                self.fixed[what] = self.fixed.get(what, 0) + n

    def count_download(self, replaced: bool) -> None:
        with self._lock:
            if replaced:
                self.redownloaded += 1
            else:
                self.downloaded += 1

    def summary(self) -> str:
        parts = []
        if self.downloaded:
            verb = "restored" if self.kind == "restore" else "new"
            parts.append(f"{self.downloaded} {verb} video{'s' if self.downloaded != 1 else ''}")
        if self.redownloaded:
            parts.append(f"{self.redownloaded} broken file{'s' if self.redownloaded != 1 else ''} replaced")
        if self.fixed:
            parts.append("fixed " + ", ".join(f"{what} ×{n}" for what, n in sorted(self.fixed.items())))
        if self.errors:
            parts.append(f"{len(self.errors)} problem{'s' if len(self.errors) != 1 else ''}")
        text = "; ".join(parts) or "Everything up to date"
        return f"{self.note} {text}".strip() if self.note else text

    def as_dict(self) -> dict:
        return {
            "name": self.name, "kind": self.kind, "status": self.status, "summary": self.summary(),
            "errors": self.errors[-5:], "finished": self.finished.strftime("%H:%M") if self.finished else "",
        }


class Context(Protocol):
    """What the engine provides to a running task (see engine.TaskContext)."""
    report: Report
    show: Show | None

    def check_cancelled(self) -> None: ...
    def set_phase(self, text: str, done: int | None = None, total: int | None = None) -> None: ...
    def show_lock(self, name: str) -> threading.Lock: ...
    def in_flight(self, show_name: str) -> set[str]: ...
    def submit(self, label: str, show: Show, fn: Callable[[], None], video_id: str | None = None,
               final: bool = False) -> None: ...


def tasks_for_all(kind: str) -> list[Task]:
    """'Update all': every subscribed show (movies never get new videos).
    'Repair all': everything currently in the library, subscribed or not."""
    entries = library.catalog(subscriptions.all())
    if kind == "repair":
        chosen = [e for e in entries if e["location"] == "library"]
    else:
        chosen = [e for e in entries if e["subscribed"] and e["mode"] != "movie"]
    return [Task(kind, url=e["url"], name=e["name"], mode=e["mode"], limit=e["limit"]) for e in chosen]


# --------------------------------------------------------------------------- inspection

@dataclass
class VideoCheck:
    video: LibraryVideo
    info: VideoInfo | None
    problem: str | None = None                       # set -> the file is broken and gets re-downloaded
    missing: set[str] = field(default_factory=set)   # metadata, subtitles, chapters, tags, thumbnail


def check_video(show: Show, video: LibraryVideo, info: VideoInfo | None, deep: bool) -> VideoCheck:
    check = VideoCheck(video, info, media.verify(video.path, info) if deep else media.find_problem(video.path, info))
    if check.problem:
        return check
    p = media.probe(video.path)
    has_subs = p.subtitle_streams > 0 or any(f.suffix == ".srt" for f, _ in library._sidecars_of(video.path))
    if info is None:
        check.missing.add("metadata")
    if not has_subs and (info is None or info.has_subtitles_for(config.settings.subtitle_languages)):
        check.missing.add("subtitles")
    if info and info.chapters and not p.chapters:
        check.missing.add("chapters")
    if info and not p.has_title_tag:
        check.missing.add("tags")
    art = library.sidecar(video.path, "-fanart.jpg" if show.is_movie else "-thumb.jpg")
    if not art.exists() and p.cover_index is None:
        check.missing.add("thumbnail")  # nothing embedded to extract it from, so download it
    return check


def inspect_library(show: Show, ctx: Context, deep: bool) -> list[VideoCheck]:
    store = show.metadata()
    videos = [v for v in library.find_videos(show, store) if v.id]
    checks = []
    for i, video in enumerate(videos, start=1):
        ctx.check_cancelled()
        ctx.set_phase(f"{'Verifying' if deep else 'Checking'} {video.path.name}", i, len(videos))
        checks.append(check_video(show, video, store.videos.get(video.id), deep))
    return checks


def find_staged_videos(show: Show) -> dict[str, Path]:
    """Finished downloads left in staging by an interrupted run (also understands the old layout)."""
    found = {}
    if show.staging_dir.is_dir():
        for path in show.staging_dir.rglob("*"):
            if path.is_file() and library.is_video_file(path) and not path.name.startswith("."):
                vid = video_id_from_name(path.name)
                if vid:
                    found[vid] = path
    return found


# --------------------------------------------------------------------------- the task

def run_task(task: Task, ctx: Context, session_factory=None) -> None:
    """Runs on the download thread. Library changes are submitted to the processing thread."""
    from . import ytdl  # imported lazily so the library code can be used without yt-dlp installed
    session_factory = session_factory or ytdl.Session
    report = ctx.report

    if task.url and not task.name:
        existing = subscriptions.find_by_url(ytdl.normalize_url(task.url)) or subscriptions.find_by_url(task.url)
        if existing:
            task.name = existing.name
    if ytdl.is_channel_url(task.url) and task.mode == "playlist":
        task.mode = "years"  # a channel's order changes with every upload; only dates are stable

    with session_factory(task.url, task.mode, ctx, task.limit) as session:
        if not task.name:
            ctx.set_phase("Looking up the name of this channel / playlist / video")
            task.name = _unique_name(session.resolve_name(), task.url)
        show = Show(task.name, task.mode)
        report.name = show.name
        ctx.show = show
        if task.url:
            subscriptions.upsert(Subscription(show.name, task.url, task.mode, task.limit))
        log.info("%s %s (%s)", task.kind.capitalize(), show.name, show.mode)

        # 1. What is there already (under the show lock: the processing thread may be busy with it).
        with ctx.show_lock(show.name):
            store = show.metadata()
            state = ShowState(show.metadata_dir)
            checks = [] if task.kind == "restore" else inspect_library(show, ctx, task.deep)
            busy = ctx.in_flight(show.name)
            staged = {vid: p for vid, p in find_staged_videos(show).items() if vid not in busy}
            deleted = library.deleted_ids(show, store) - busy - staged.keys()

        for vid, path in staged.items():
            log.info("%s: resuming processing of %s", show.name, path.name)
            ctx.submit(f"Processing {path.name}", show, lambda p=path: process_download(show, p, None, report), vid)

        broken = {c.video.id: c.problem for c in checks if c.problem}
        for vid, problem in broken.items():
            report.error(f"{vid}: {problem} - downloading it again")
        known = ({c.video.id for c in checks} | set(store.videos) | staged.keys() | busy) - broken.keys()

        handed_off: set[str] = set()

        def on_video(path: Path, info: dict) -> None:
            vid = info["id"]
            handed_off.add(vid)
            ctx.submit(f"Processing {info.get('title') or vid}", show,
                       lambda: process_download(show, path, info, report, replaced=vid in broken), vid)

        if task.kind == "restore":
            if deleted:
                ctx.set_phase(f"Restoring {len(deleted)} deleted video{'s' if len(deleted) != 1 else ''}")
                session.download_ids(sorted(deleted), show.staging_dir, known - deleted, on_video)
            else:
                report.note = "Nothing to restore."
            submit_finalize(ctx, show)
            return

        # 2. Missing extras for videos in the library: fetch them here, fix them on the processing thread.
        _fix_existing(task, ctx, show, checks, state, busy)

        # 3. New videos, then replacements for broken files.
        listed_everything = False
        if task.url:
            listed_everything = _download_new(task, ctx, session, show, store, state, known, on_video)
        elif not broken:
            report.note = "No source URL known, so only the existing files were checked."
        leftover = [vid for vid in broken if vid not in handed_off]
        if leftover:
            ctx.set_phase("Re-downloading broken videos")
            session.download_ids(leftover, show.staging_dir, known, on_video)

    submit_finalize(ctx, show, refresh_art=task.deep, listing_complete=listed_everything)


def _fix_existing(task: Task, ctx: Context, show: Show, checks: list[VideoCheck], state: ShowState,
                  busy: set[str]) -> None:
    from . import ytdl
    report = ctx.report
    to_fix = {}
    for c in checks:
        if c.problem or c.video.id in busy:
            continue
        missing = set(c.missing)
        if "subtitles" in missing and task.kind == "update" and state.subtitles_recently_failed(c.video.id):
            missing.discard("subtitles")  # failed recently; try again in a few days (or with Repair)
        if missing or task.refresh_metadata:
            to_fix[c.video.id] = (c, missing)

    fetch = {vid: (missing & {"metadata", "subtitles"}) | ({"metadata"} if task.refresh_metadata else set())
             for vid, (_, missing) in to_fix.items()}
    fetch = {vid: needs for vid, needs in fetch.items() if needs}
    if fetch:
        progress = iter(range(1, len(fetch) + 1))
        phase = "Fetching missing metadata / subtitles"
        ctx.set_phase(phase, 0, len(fetch))
        ytdl.fetch_extras(fetch, show.staging_dir, ctx,
                          on_done=lambda _vid: ctx.set_phase(phase, next(progress), len(fetch)))
    thumbs = [vid for vid, (_, missing) in to_fix.items() if "thumbnail" in missing]
    for i, vid in enumerate(thumbs, start=1):
        ctx.check_cancelled()
        ctx.set_phase("Fetching missing thumbnails", i, len(thumbs))
        info = to_fix[vid][0].info
        if not ytdl.download_thumbnail(vid, info.thumbnail if info else "", show.staging_dir / f"{vid}.jpg"):
            report.error(f"{vid}: the thumbnail could not be downloaded")

    for vid, (check, missing) in to_fix.items():
        ctx.submit(f"Fixing {check.video.path.name}", show,
                   lambda v=vid, m=missing: fix_video(show, v, m, report), vid)


def _download_new(task: Task, ctx: Context, session, show: Show, store, state: ShowState, known: set[str],
                  on_video: Callable[[Path, dict], None]) -> bool:
    """Downloads videos the archive has never had. Returns True when the whole source was listed."""
    from . import ytdl
    if not show.is_movie and ytdl.is_channel_url(task.url) and task.kind == "update" and state.complete:
        # Cheap path for channels that were fully listed before: one RSS request, and only if that
        # shows something new, a listing that stops at the first video we already have.
        channel_id = store.channel_id()
        recent = ytdl.recent_upload_ids(channel_id) if channel_id else None
        if recent is not None and all(vid in known for vid in recent):
            ctx.report.note = "Checked the channel's feed:"
            return False
        ctx.set_phase("Checking for new videos")
        session.download(show.staging_dir, known, on_video, incremental=True)
        return False
    ctx.set_phase("Checking for new videos")
    session.download(show.staging_dir, known, on_video)
    return True


def submit_finalize(ctx: Context, show: Show, refresh_art: bool = False, listing_complete: bool = False) -> None:
    ctx.submit(f"Finishing {show.name}", show,
               lambda: finalize_show(show, ctx.report, refresh_art, listing_complete), final=True)


def _unique_name(name: str, url: str) -> str:
    """Avoids merging two different sources that happen to share a title into one folder."""
    candidate, n = name, 2
    while (sub := subscriptions.get(candidate)) and sub.url.rstrip("/") != url.rstrip("/"):
        candidate, n = f"{name} ({n})", n + 1
    return candidate


# --------------------------------------------------------------------------- processing jobs

def _staged_file(staging: Path, vid: str, suffixes: tuple[str, ...]) -> Path | None:
    for suffix in suffixes:
        path = staging / f"{vid}{suffix}"
        if path.exists():
            return path
    return None


def _staged_subtitle(staging: Path, vid: str) -> tuple[Path, str] | None:
    for lang in config.settings.subtitle_languages:
        path = staging / f"{vid}.{lang}.srt"
        if path.exists():
            return path, lang
    return None


def _clear_staged(staging: Path, vid: str) -> None:
    """Removes everything staged for a video once it is safely in the library, including
    leftover partial streams from earlier failed attempts."""
    if staging.is_dir():
        for path in staging.glob(f"{vid}.*"):
            path.unlink(missing_ok=True)


def _save_staged_info(show: Show, vid: str, info: dict | None) -> VideoInfo | None:
    raw = info or read_json(show.staging_dir / f"{vid}.info.json") \
        or read_json(show.staging_dir / config.METADATA_DIR_NAME / f"{vid}.info.json")
    store = show.metadata()
    if raw:
        show.metadata_dir.mkdir(parents=True, exist_ok=True)
        return store.save_video(raw)
    return store.videos.get(vid)


def process_download(show: Show, staged_video: Path, info: dict | None, report: Report, replaced: bool = False) -> None:
    """A freshly downloaded video: one remux pass adds tags, chapters, subtitles and cover art while
    writing straight into the library, then the show is re-organized (names, NFOs, artwork)."""
    vid = video_id_from_name(staged_video.name)
    staging = show.staging_dir
    show.library_dir.mkdir(parents=True, exist_ok=True)
    meta = _save_staged_info(show, vid, info)
    subtitle = _staged_subtitle(staging, vid)
    thumb = _staged_file(staging, vid, _IMAGE_EXTS)
    cover = None
    if thumb:
        cover = staging / f"{vid}.cover.jpg"
        if not media.to_jpeg(thumb, cover):
            cover = None

    tmp = show.library_dir / f".tmp-{vid}.mkv"
    try:
        media.remux(staged_video, tmp, info=meta, subtitle=subtitle[0] if subtitle else None,
                    subtitle_lang=subtitle[1] if subtitle else "en", cover=cover)
        staged_video.unlink(missing_ok=True)
    except media.MediaError as e:
        report.error(f"{vid}: could not embed extras ({e}); keeping the video as downloaded")
        tmp = show.library_dir / f".tmp-{vid}{staged_video.suffix.lower()}"
        move_file(staged_video, tmp)

    library.organize(show, incoming={vid: tmp}, thumbnails={vid: cover} if cover else None)
    _clear_staged(staging, vid)
    report.count_download(replaced)
    log.info("%s: added %s", show.name, (meta.title if meta else vid))


def fix_video(show: Show, vid: str, missing: set[str], report: Report) -> None:
    """Adds whatever an existing library video lacks, in at most one remux pass."""
    staging = show.staging_dir
    meta = _save_staged_info(show, vid, None)
    video = next((v.path for v in library.find_videos(show) if v.id == vid), None)
    if video is None:
        return
    if "metadata" in missing and meta:
        report.count_fix("metadata")

    p = media.probe(video)
    subtitle = _staged_subtitle(staging, vid) if not p.subtitle_streams else None
    if "subtitles" in missing and meta and meta.has_subtitles_for(config.settings.subtitle_languages):
        state = ShowState(show.metadata_dir)
        state.record_subtitles(vid, ok=subtitle is not None)
        state.save()
        if not subtitle:
            report.error(f"{video.name}: subtitles could not be downloaded (YouTube may be rate-limiting; "
                         f"they will be tried again in {ShowState.SUBTITLE_RETRY_DAYS} days or by Repair)")
    thumb = _staged_file(staging, vid, _IMAGE_EXTS)
    add_chapters = bool(meta and meta.chapters and not p.chapters)
    add_tags = bool(meta and not p.has_title_tag)

    if subtitle or add_chapters or add_tags:
        out_ext = video.suffix.lower() if video.suffix.lower() in (".mkv", ".mp4") else ".mkv"
        tmp = video.parent / f".tmp-{vid}{out_ext}"
        cover = None
        if thumb and p.cover_index is None:
            cover = staging / f"{vid}.cover.jpg"
            cover = cover if media.to_jpeg(thumb, cover) else None
        try:
            media.remux(video, tmp, info=meta, subtitle=subtitle[0] if subtitle else None,
                        subtitle_lang=subtitle[1] if subtitle else "en", cover=cover)
            final = video.with_suffix(out_ext)
            move_file(tmp, final)
            if final != video:
                video.unlink(missing_ok=True)
            for what, done in (("subtitles", subtitle), ("chapters", add_chapters), ("tags", add_tags)):
                if done:
                    report.count_fix(what)
        except media.MediaError as e:
            tmp.unlink(missing_ok=True)
            report.error(f"{video.name}: {e}")

    result = library.organize(show, thumbnails={vid: thumb} if thumb else None)
    report.count_fix("artwork", result.artwork_created)
    _clear_staged(staging, vid)


def finalize_show(show: Show, report: Report, refresh_art: bool = False, listing_complete: bool = False) -> None:
    """Last job of a task: stores playlist info/artwork, runs a final full organize pass and records
    that the source was listed completely (which lets later updates take the cheap path)."""
    staging = show.staging_dir
    playlist_raw = read_json(staging / "playlist.info.json")
    if playlist_raw and show.library_dir.is_dir():
        show.metadata().save_playlist(playlist_raw)
    playlist_art = _staged_file(staging, "playlist", _IMAGE_EXTS)
    if show.library_dir.is_dir():
        result = library.organize(show, playlist_art=playlist_art, refresh_art=refresh_art)
        report.count_fix("nfo files", result.nfos_written)
        report.count_fix("artwork", result.artwork_created)
        if result.unidentified:
            report.error(f"Left alone (no YouTube ID found): {', '.join(result.unidentified[:5])}")
        if listing_complete:
            state = ShowState(show.metadata_dir)
            state.complete = True
            state.save()
    if staging.is_dir():
        for path in staging.glob("playlist.*"):
            path.unlink(missing_ok=True)
        remove_empty_dirs(staging)
        try:
            staging.rmdir()
        except OSError:
            pass  # partial downloads are kept so the next run can resume them
