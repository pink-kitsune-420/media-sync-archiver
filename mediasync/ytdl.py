"""Everything that talks to YouTube lives in this module.

The app is meant to archive, not to hammer YouTube, so requests are kept to a minimum:
  * videos already in the archive are never downloaded again;
  * a channel update first reads the channel's RSS feed (one small request) and only lists the
    channel when that shows something new, stopping at the first video it already has;
  * missing thumbnails come straight from YouTube's image server (no video page lookup);
  * metadata and subtitles are only fetched for videos that are missing them;
  * with "gentle" mode on (the default) yt-dlp pauses between requests like its own "sleep" preset.

Downloads go into a staging folder named by video ID (<id>.mkv, <id>.info.json, <id>.en.srt,
<id>.jpg). As soon as yt-dlp has finished a video it is handed to a callback, so the processing
thread can embed/organize it while the next one downloads.
"""
from __future__ import annotations

import contextlib
import logging
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Protocol

import yt_dlp
from yt_dlp.postprocessor.common import PostProcessor
from yt_dlp.utils import DownloadCancelled, ExistingVideoReached, parse_bytes

from . import __version__, config
from .logs import DETAIL, YTDLP_LOGGER
from .metadata import slim_info
from .util import sanitize_filename, truncate_utf8, write_atomic

log = logging.getLogger(__name__)
ytdlp_log = logging.getLogger(YTDLP_LOGGER)

_CHANNEL_MARKERS = ("/@", "/channel/", "/user/", "/c/")
_CHANNEL_TABS = ("/videos", "/shorts", "/live", "/streams", "/playlists")
_USER_AGENT = f"MediaSyncArchiver/{__version__} (+personal offline archive)"
_FEED_NS = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
_UNAVAILABLE_TITLES = ("[Private video]", "[Deleted video]")  # how playlists list videos nobody can watch


class TaskHooks(Protocol):
    """What a running task provides to the downloader (implemented by engine.TaskContext)."""

    def check_cancelled(self) -> None: ...
    def on_progress(self, d: dict) -> None: ...
    def attach_downloader(self, ydl: yt_dlp.YoutubeDL | None) -> None: ...
    def ratelimit(self) -> int | None: ...
    def warning(self, message: str) -> None: ...
    def error(self, message: str) -> None: ...


def is_channel_url(url: str | None) -> bool:
    return bool(url) and any(marker in url for marker in _CHANNEL_MARKERS) and "list=" not in url


def normalize_url(url: str) -> str:
    """Channel URLs point at their /videos tab so yt-dlp lists uploads rather than the home page."""
    url = url.strip().rstrip("/")
    if is_channel_url(url) and not url.endswith(_CHANNEL_TABS):
        url += "/videos"
    return url


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def parse_rate(text: str | None) -> int | None:
    """'500K' / '2M' / '1.5M' -> bytes per second (None means unlimited)."""
    return parse_bytes(text) if text else None


# --------------------------------------------------------------------------- plain HTTP requests

def _http_get(url: str, timeout: float = 20) -> bytes | None:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read()
    except (urllib.error.URLError, OSError, ValueError) as e:
        log.debug("GET %s failed: %s", url, e)
        return None
    log.debug("GET %s (%d bytes)", url, len(data))
    return data


def recent_upload_ids(channel_id: str) -> list[str] | None:
    """The newest uploads of a channel (at most 15, Shorts excluded) from its RSS feed, in one
    small request. None when the feed cannot be read."""
    data = _http_get(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}")
    if data is None:
        return None
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    ids = []
    for entry in root.findall("atom:entry", _FEED_NS):
        video_id = entry.findtext("yt:videoId", namespaces=_FEED_NS)
        link = entry.find("atom:link", _FEED_NS)
        if video_id and "/shorts/" not in (link.get("href", "") if link is not None else ""):
            ids.append(video_id)
    return ids


def download_thumbnail(video_id: str, url: str, dst: Path) -> bool:
    """Saves a video's thumbnail from YouTube's image server (static files, no page lookup)."""
    candidates = [u for u in (url, f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg",
                              f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg") if u]
    for candidate in dict.fromkeys(candidates):
        data = _http_get(candidate)
        if data and len(data) > 1024:  # i.ytimg.com answers missing sizes with a tiny placeholder
            write_atomic(dst, data)
            return True
    return False


# --------------------------------------------------------------------------- yt-dlp

class _Logger:
    """yt-dlp's messages go to the log file only; its errors also go to the task report."""

    def __init__(self, hooks: TaskHooks):
        self.hooks = hooks

    def debug(self, msg: str) -> None:
        ytdlp_log.debug("yt-dlp: %s", msg)

    info = debug

    def warning(self, msg: str) -> None:
        ytdlp_log.warning("yt-dlp: %s", msg.removeprefix("WARNING: "))
        self.hooks.warning(msg)

    def error(self, msg: str) -> None:
        msg = msg.removeprefix("ERROR: ")
        ytdlp_log.error("yt-dlp: %s", msg)
        self.hooks.error(msg)


def _skip_unavailable(info: dict, *, incomplete: bool) -> str | None:
    """yt-dlp match_filter: playlist entries that YouTube already lists as private or deleted are
    skipped without requesting them (they would only fail, on every update)."""
    if info.get("title") in _UNAVAILABLE_TITLES:
        log.log(DETAIL, "Skipping %s: listed as %s", info.get("id"), info["title"])
        return f"{info.get('id')} is listed as {info['title']}"
    return None


class _CancelCheck(PostProcessor):
    """Runs before each video so pause/stop also works between downloads, not just during them."""

    def __init__(self, hooks: TaskHooks):
        super().__init__()
        self.hooks = hooks

    def run(self, info):
        self.hooks.check_cancelled()
        return [], info


class _Handoff(PostProcessor):
    """Called once a video is completely downloaded and merged; passes it on for processing."""

    def __init__(self, callback: Callable[[Path, dict], None]):
        super().__init__()
        self.callback = callback

    def run(self, info):
        self.callback(Path(info["filepath"]), slim_info(info))
        return [], info


def _base_options(hooks: TaskHooks, staging: Path) -> dict:
    opts = {
        "paths": {"home": str(staging)},
        "outtmpl": {"default": "%(id)s.%(ext)s", "pl_infojson": "playlist", "pl_thumbnail": "playlist"},
        "writeinfojson": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": list(config.settings.subtitle_languages),
        "postprocessors": [
            {"key": "FFmpegSubtitlesConvertor", "format": "srt", "when": "before_dl"},
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"},
        ],
        "js_runtimes": {name: dict(opts) for name, opts in config.JS_RUNTIMES.items()},
        "ignoreerrors": True,  # one unavailable video must not abort a whole playlist
        "match_filter": _skip_unavailable,
        "quiet": True,
        "noprogress": True,
        "color": "no_color",  # plain text in the log file, even when started from a terminal
        "logger": _Logger(hooks),
        "progress_hooks": [hooks.on_progress],
        "ratelimit": hooks.ratelimit(),
    }
    if config.settings.gentle:
        opts.update(config.GENTLE_SLEEP)
    return opts


class Session:
    """One yt-dlp session per task: optionally resolves the source name, then downloads everything
    that is not in the archive yet. The same extraction result is reused for both steps."""

    def __init__(self, url: str | None, mode: str, hooks: TaskHooks, limit: int | None = None):
        self.url = normalize_url(url) if url else None
        self.mode = mode
        self.hooks = hooks
        self._archive: set[str] = set()
        self._resolved: dict | None = None
        self._on_video: Callable[[Path, dict], None] | None = None
        opts = _base_options(hooks, config.STAGING_DIR)
        opts.update({
            "format": config.video_format(),
            "merge_output_format": "mkv",
            "writethumbnail": True,
            "download_archive": self._archive,  # in-memory archive: every video the library knows about
            "noplaylist": mode == "movie",
            "playlistend": limit,
        })
        self.ydl = yt_dlp.YoutubeDL(opts)
        self.ydl.add_post_processor(_CancelCheck(hooks), when="pre_process")
        self.ydl.add_post_processor(_Handoff(self._handoff), when="after_move")

    def __enter__(self) -> Session:
        self.ydl.__enter__()
        return self

    def __exit__(self, *exc) -> None:
        self.ydl.__exit__(*exc)

    def _handoff(self, path: Path, info: dict) -> None:
        if self._on_video:
            self._on_video(path, info)

    def resolve_name(self) -> str:
        """Folder name for the source: video title (movies), channel name or playlist title."""
        info = self.ydl.extract_info(self.url, download=False, process=False)
        for _ in range(3):
            if not info or info.get("_type") not in ("url", "url_transparent"):
                break
            info = self.ydl.extract_info(info["url"], download=False, process=False, ie_key=info.get("ie_key"))
        if not info:
            raise RuntimeError(f"yt-dlp could not read {self.url}")
        self._resolved = info
        if self.mode == "movie":
            raw = info.get("title")
        elif is_channel_url(self.url):
            raw = info.get("channel") or info.get("uploader") or info.get("title")
        else:
            raw = info.get("title") or info.get("channel")
        name = truncate_utf8(sanitize_filename(raw or ""), 100)
        if not name:
            raise RuntimeError(f"Could not work out a name for {self.url}")
        return name

    def download(self, staging: Path, known_ids: set[str], on_video: Callable[[Path, dict], None],
                 incremental: bool = False) -> None:
        """Downloads every video of the source whose ID is not in known_ids into staging.
        incremental: walk the (newest-first) list lazily and stop at the first known video."""
        self.ydl.params["lazy_playlist"] = incremental
        self.ydl.params["break_on_existing"] = incremental
        with self._downloading(staging, known_ids, on_video):
            try:
                if self._resolved is not None:
                    self.ydl.process_ie_result(self._resolved, download=True)
                else:
                    self.ydl.download([self.url])
            except ExistingVideoReached:
                log.debug("Reached a video that is already archived; stopping the listing there")

    def download_ids(self, ids: list[str], staging: Path, known_ids: set[str],
                     on_video: Callable[[Path, dict], None]) -> None:
        """Downloads specific videos (restored or broken ones, even if they left the playlist)."""
        self.ydl.params["lazy_playlist"] = self.ydl.params["break_on_existing"] = False
        with self._downloading(staging, known_ids, on_video):
            self.ydl.download([watch_url(vid) for vid in ids])

    @contextlib.contextmanager
    def _downloading(self, staging: Path, known_ids: set[str], on_video: Callable[[Path, dict], None]):
        staging.mkdir(parents=True, exist_ok=True)
        self.ydl.params["paths"] = {"home": str(staging)}
        self._archive.clear()
        self._archive.update(f"youtube {vid}" for vid in known_ids)
        self._on_video = on_video
        self.hooks.attach_downloader(self.ydl)
        try:
            yield
        finally:
            self.hooks.attach_downloader(None)
            self._on_video = None


def fetch_extras(needs: dict[str, set[str]], staging: Path, hooks: TaskHooks,
                 on_done: Callable[[str], None] | None = None) -> None:
    """Fetches info.json (always) and, where listed in needs, subtitles for videos already in the
    library, without downloading the videos themselves."""
    staging.mkdir(parents=True, exist_ok=True)
    groups: dict[bool, list[str]] = {}
    for vid, wanted in needs.items():
        groups.setdefault("subtitles" in wanted, []).append(vid)
    for subs, ids in groups.items():
        opts = _base_options(hooks, staging)
        opts.update({"skip_download": True, "noplaylist": True,
                     "writesubtitles": subs, "writeautomaticsub": subs, "writethumbnail": False})
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.add_post_processor(_CancelCheck(hooks), when="pre_process")
            for vid in ids:
                hooks.check_cancelled()
                try:
                    ydl.download([watch_url(vid)])
                except DownloadCancelled:
                    raise
                except Exception as e:  # noqa: BLE001 - keep going with the other videos
                    hooks.error(f"{vid}: {e}")
                if on_done:
                    on_done(vid)
