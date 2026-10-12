"""Video and playlist metadata: reading yt-dlp .info.json files and storing compact copies of them."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from .util import read_json, write_json

# Keys kept when an .info.json is stored in the library. Raw yt-dlp files are often 1-10 MB
# because of format/caption URL lists that expire within hours anyway.
_KEEP_KEYS = (
    "id", "title", "fulltitle", "description", "upload_date", "timestamp", "release_date", "duration",
    "channel", "channel_id", "channel_url", "uploader", "uploader_id", "uploader_url",
    "webpage_url", "thumbnail", "chapters", "categories", "tags", "view_count", "like_count",
    "playlist", "playlist_id", "playlist_title", "playlist_index", "playlist_count",
    "extractor", "extractor_key", "_type", "ext", "width", "height", "fps", "vcodec", "acodec",
    "language", "age_limit", "availability", "live_status", "playlist_uploader",
)


# Where a video sits in its playlist. Only known when it was fetched through the playlist, so a
# refresh via the plain video URL must keep the stored values (otherwise episodes get renumbered).
_PLAYLIST_KEYS = ("playlist", "playlist_id", "playlist_title", "playlist_index", "playlist_count")


def _subtitle_langs(raw: dict) -> list[str]:
    if "subtitle_langs" in raw:
        return list(raw.get("subtitle_langs") or [])
    return sorted(set(raw.get("subtitles") or {}) | set(raw.get("automatic_captions") or {}))


def slim_info(raw: dict) -> dict:
    """Returns the subset of a yt-dlp info dict worth keeping on disk."""
    data = {key: raw[key] for key in _KEEP_KEYS if raw.get(key) is not None}
    if raw.get("_type", "video") == "video":
        data["subtitle_langs"] = _subtitle_langs(raw)
        data["chapters"] = [
            {"start_time": c.get("start_time"), "end_time": c.get("end_time"), "title": c.get("title")}
            for c in raw.get("chapters") or []
        ]
    return data


@dataclass
class VideoInfo:
    id: str
    title: str = ""
    description: str = ""
    upload_date: str = ""
    timestamp: int | None = None
    duration: float | None = None
    channel: str = ""
    channel_id: str = ""
    webpage_url: str = ""
    thumbnail: str = ""
    chapters: list[dict] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    playlist_index: int | None = None
    subtitle_langs: list[str] = field(default_factory=list)
    has_audio: bool = True

    @classmethod
    def from_json(cls, raw: dict) -> VideoInfo | None:
        if not raw.get("id") or raw.get("_type", "video") != "video":
            return None
        upload_date = str(raw.get("upload_date") or raw.get("release_date") or "")
        return cls(
            id=raw["id"],
            title=raw.get("title") or raw.get("fulltitle") or "",
            description=raw.get("description") or "",
            upload_date=upload_date if len(upload_date) == 8 and upload_date.isdigit() else "",
            timestamp=raw.get("timestamp"),
            duration=raw.get("duration"),
            channel=raw.get("channel") or raw.get("uploader") or "",
            channel_id=raw.get("channel_id") or "",
            webpage_url=raw.get("webpage_url") or f"https://www.youtube.com/watch?v={raw['id']}",
            thumbnail=raw.get("thumbnail") or "",
            chapters=[c for c in raw.get("chapters") or [] if c.get("start_time") is not None],
            categories=[c for c in raw.get("categories") or [] if c],
            tags=[t for t in raw.get("tags") or [] if t],
            playlist_index=raw.get("playlist_index"),
            subtitle_langs=_subtitle_langs(raw),
            has_audio=raw.get("acodec") != "none",
        )

    @property
    def year(self) -> str:
        return self.upload_date[:4]

    @property
    def aired(self) -> str:
        """Upload date as YYYY-MM-DD, or "" when unknown."""
        d = self.upload_date
        return f"{d[:4]}-{d[4:6]}-{d[6:]}" if d else ""

    def has_subtitles_for(self, langs: list[str]) -> bool:
        return any(lang in self.subtitle_langs for lang in langs)


@dataclass
class PlaylistInfo:
    id: str
    title: str = ""
    description: str = ""
    channel: str = ""
    channel_id: str = ""
    webpage_url: str = ""
    thumbnail_url: str = ""

    @classmethod
    def from_json(cls, raw: dict) -> PlaylistInfo | None:
        if raw.get("_type") != "playlist" or not raw.get("id"):
            return None
        thumbs = [t for t in raw.get("thumbnails") or [] if t.get("url")]
        return cls(
            id=raw["id"],
            title=raw.get("title") or "",
            description=raw.get("description") or "",
            channel=raw.get("channel") or raw.get("uploader") or "",
            channel_id=raw.get("channel_id") or "",
            webpage_url=raw.get("webpage_url") or "",
            thumbnail_url=thumbs[-1]["url"] if thumbs else "",
        )


# Parsed .info.json files keyed by path, invalidated by mtime/size. Legacy files can be
# 10 MB each, so each one is only parsed once per process.
_parse_cache: dict[str, tuple[int, int, VideoInfo | PlaylistInfo | None]] = {}
_parse_lock = threading.Lock()


def load_info_file(path: Path) -> VideoInfo | PlaylistInfo | None:
    try:
        st = path.stat()
    except OSError:
        return None
    key = str(path)
    with _parse_lock:
        cached = _parse_cache.get(key)
        if cached and cached[:2] == (st.st_mtime_ns, st.st_size):
            return cached[2]
    raw = read_json(path) or {}
    parsed = VideoInfo.from_json(raw) or PlaylistInfo.from_json(raw)
    with _parse_lock:
        _parse_cache[key] = (st.st_mtime_ns, st.st_size, parsed)
    return parsed


class MetadataStore:
    """The .metadata folder of one show or movie: one <id>.info.json per video plus playlist.info.json."""

    PLAYLIST_FILE = "playlist.info.json"

    def __init__(self, directory: Path):
        self.directory = directory
        self.videos: dict[str, VideoInfo] = {}
        self.playlist: PlaylistInfo | None = None
        self._files: dict[str, list[Path]] = {}
        self._playlist_files: list[Path] = []
        if directory.is_dir():
            # Oldest first so the newest file wins when legacy duplicates exist.
            paths = sorted(directory.glob("*.info.json"), key=lambda p: p.stat().st_mtime_ns)
            for path in paths:
                parsed = load_info_file(path)
                if isinstance(parsed, VideoInfo):
                    self.videos[parsed.id] = parsed
                    self._files.setdefault(parsed.id, []).append(path)
                elif isinstance(parsed, PlaylistInfo):
                    self.playlist = parsed
                    self._playlist_files.append(path)

    def save_video(self, raw: dict) -> VideoInfo | None:
        if raw.get("id") and raw.get("playlist_index") is None:
            stored = next((d for d in map(read_json, reversed(self._files.get(raw["id"], []))) if d), None)
            if stored:
                raw = {**raw, **{k: stored[k] for k in _PLAYLIST_KEYS if stored.get(k) is not None}}
        info = VideoInfo.from_json(raw)
        if not info:
            return None
        target = self.directory / f"{info.id}.info.json"
        write_json(target, slim_info(raw))
        for old in self._files.get(info.id, []):
            if old != target:
                old.unlink(missing_ok=True)
        self._files[info.id] = [target]
        self.videos[info.id] = info
        return info

    def save_playlist(self, raw: dict) -> None:
        info = PlaylistInfo.from_json(raw)
        if not info:
            return
        target = self.directory / self.PLAYLIST_FILE
        write_json(target, slim_info(raw))
        for old in self._playlist_files:
            if old != target:
                old.unlink(missing_ok=True)
        self._playlist_files = [target]
        self.playlist = info

    def remove_video(self, video_id: str) -> None:
        for path in self._files.pop(video_id, []):
            path.unlink(missing_ok=True)
        self.videos.pop(video_id, None)

    def channel_id(self) -> str:
        """The YouTube channel ID of this source, if any stored metadata knows it."""
        if self.playlist and self.playlist.channel_id:
            return self.playlist.channel_id
        return next((v.channel_id for v in self.videos.values() if v.channel_id), "")


class ShowState:
    """Bookkeeping for one show in .metadata/state.json.

    complete:          a full listing of the source has finished once, so later updates may stop at
                       the first video already in the archive instead of listing everything again.
    subtitle_failures: video ID -> date of the last failed subtitle download, so YouTube isn't asked
                       again on every update (retried after SUBTITLE_RETRY_DAYS, or by Repair).
    """

    FILE = "state.json"
    SUBTITLE_RETRY_DAYS = 7

    def __init__(self, metadata_dir: Path):
        self.path = metadata_dir / self.FILE
        data = read_json(self.path) or {}
        self.complete = bool(data.get("complete"))
        self.subtitle_failures: dict[str, str] = dict(data.get("subtitle_failures") or {})

    def subtitles_recently_failed(self, video_id: str) -> bool:
        failed = self.subtitle_failures.get(video_id)
        try:
            return bool(failed) and date.fromisoformat(failed) > date.today() - timedelta(days=self.SUBTITLE_RETRY_DAYS)
        except ValueError:
            return False

    def record_subtitles(self, video_id: str, ok: bool) -> None:
        if ok:
            self.subtitle_failures.pop(video_id, None)
        else:
            self.subtitle_failures[video_id] = date.today().isoformat()

    def save(self) -> None:
        if self.path.parent.is_dir():
            write_json(self.path, {"complete": self.complete, "subtitle_failures": self.subtitle_failures}, indent=2)
