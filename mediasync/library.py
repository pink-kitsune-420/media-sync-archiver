"""The library on disk: finding videos, naming and numbering them, NFOs, artwork and junk cleanup.

Layout (works with Kodi and Jellyfin):
  Shows/<Show>/tvshow.nfo, poster.jpg, fanart.jpg, landscape.jpg, season01-poster.jpg ...
  Shows/<Show>/Season 01/S01E03 - <Title> [<id>].mkv (+ .nfo, -thumb.jpg)            playlist order
  Shows/<Show>/Season 2021/S2021E031501 - <Title> [<id>].mkv (+ .nfo, -thumb.jpg)    by upload date
  Movies/<Movie>/<Title> [<id>].mkv (+ .nfo, -poster.jpg, -fanart.jpg)
  <Show or Movie>/.metadata/<id>.info.json, playlist.info.json, playlist.jpg, state.json

By-year shows number episodes by upload date (S<year>E<month><day><n>), so the numbers never
change when older or newer videos are added later.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import artwork, config, media, nfo
from .logs import DETAIL
from .metadata import MetadataStore, VideoInfo
from .folders import folders
from .subscriptions import subscriptions
from .util import (
    format_bytes, is_fragment, move_file, remove_empty_dirs, sanitize_filename, truncate_utf8,
    video_id_from_name, write_text_if_changed,
)

log = logging.getLogger(__name__)

_SIDECAR_RE = re.compile(r"(\.nfo|-thumb\.jpg|-poster\.jpg|-fanart\.jpg|(\.[\w-]+)?\.srt)$")
_EXT_PREFERENCE = {".mkv": 0, ".mp4": 1, ".webm": 2}
_SEASON_YEAR_RE = re.compile(r"^Season (\d{4})$")
_EPISODE_RE = re.compile(r"\bS\d+E(\d+)\b", re.IGNORECASE)
_DATE_EPISODE_RE = re.compile(r"\bS(\d{4})E(\d{4})\d{2}\b", re.IGNORECASE)
PLAYLIST_ART = "playlist.jpg"


class LibraryOffline(Exception):
    """The library a show lives in is not connected (its drive is unplugged)."""


class MoveError(Exception):
    """A show could not be moved to another library (nothing was changed)."""


@dataclass(frozen=True)
class Show:
    """One library entry: a TV show (playlist/years mode) or a movie folder."""
    name: str
    mode: str
    library: str = ""  # name of the library it is in; "" = the default library

    @property
    def is_movie(self) -> bool:
        return self.mode == "movie"

    @property
    def lib(self) -> config.Library:
        return config.get_library(self.library) or config.default_library()

    @property
    def library_dir(self) -> Path:
        return self.lib.path_for(self.is_movie) / self.name

    def require_online(self) -> None:
        """Called before writing to the library, so nothing is ever created where an unplugged drive was."""
        if not self.lib.online:
            raise LibraryOffline(f"{self.lib.name} is not connected")

    @property
    def staging_dir(self) -> Path:
        return config.STAGING_DIR / self.name

    @property
    def metadata_dir(self) -> Path:
        return self.library_dir / config.METADATA_DIR_NAME

    def metadata(self) -> MetadataStore:
        return MetadataStore(self.metadata_dir)


@dataclass
class LibraryVideo:
    path: Path
    id: str | None


@dataclass
class Placement:
    video: LibraryVideo
    info: VideoInfo | None
    target: Path
    title: str
    season: int = 0
    episode: int = 0


@dataclass
class OrganizeResult:
    moved: int = 0
    renamed: int = 0  # files that were already in the library (moved also counts new arrivals)
    removed: list[str] = field(default_factory=list)
    nfos_written: int = 0
    artwork_created: int = 0
    unidentified: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.moved or self.removed or self.nfos_written or self.artwork_created)


# --------------------------------------------------------------------------- discovery

def infer_mode(library_dir: Path, is_movie: bool) -> str:
    if is_movie:
        return "movie"
    if any(_SEASON_YEAR_RE.match(p.name) for p in library_dir.glob("Season *") if p.is_dir()):
        return "years"
    return "playlist"


def iter_files(root: Path):
    """All files below root, skipping hidden files/folders such as .metadata and temp files."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if not name.startswith("."):
                yield Path(dirpath) / name


def is_video_file(path: Path) -> bool:
    return path.suffix.lower() in config.VIDEO_EXTENSIONS and not is_fragment(path.name)


def normalize_title(text: str, show_name: str = "") -> str:
    """Lower-case alphanumerics only, without show name, SxxExx tags or [id], for fuzzy-free matching."""
    t = text.lower()
    if show_name:
        t = t.replace(show_name.lower(), "")
    t = re.sub(r"s\d+e\d+", "", t)
    t = re.sub(r"\[[a-z0-9_-]{11}\]", "", t)
    return re.sub(r"[^a-z0-9]", "", t)


def _id_from_nfo(nfo_path: Path) -> str | None:
    try:
        text = nfo_path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r"<uniqueid[^>]*>([A-Za-z0-9_-]{11})</uniqueid>", text)
    return m.group(1) if m else None


def find_videos(show: Show, store: MetadataStore | None = None) -> list[LibraryVideo]:
    if not show.library_dir.is_dir():
        return []
    store = store or show.metadata()
    by_title = {normalize_title(i.title, show.name): i.id for i in store.videos.values() if i.title}
    videos = []
    for path in iter_files(show.library_dir):
        if not is_video_file(path):
            continue
        vid = (video_id_from_name(path.name) or _id_from_nfo(path.with_suffix(".nfo"))
               or by_title.get(normalize_title(path.stem, show.name)))
        videos.append(LibraryVideo(path, vid))
    return videos


def video_ids(show: Show) -> set[str]:
    return {v.id for v in find_videos(show) if v.id}


def deleted_ids(show: Show, store: MetadataStore | None = None) -> set[str]:
    """Videos that were downloaded once (their metadata is kept) but whose file is gone. Updates
    leave these alone ("download once"); the Restore action downloads them again."""
    store = store or show.metadata()
    return set(store.videos) - {v.id for v in find_videos(show, store) if v.id}


# --------------------------------------------------------------------------- naming

def _strip_show_prefix(title: str, show_name: str) -> str:
    title = re.sub(r"\s*\[[A-Za-z0-9_-]{11}\]\s*$", "", title)
    title = re.sub(rf"^{re.escape(show_name)}\s*-\s*S\d+E\d+\s*-\s*", "", title, flags=re.IGNORECASE)
    title = re.sub(r"^\s*S\d+E\d+\s*-\s*", "", title, flags=re.IGNORECASE)
    if show_name:
        title = re.sub(rf"^{re.escape(show_name)}\s*[-:]?\s*", "", title, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", title).strip()


def episode_title(raw_title: str, show_name: str) -> str:
    """Episode title for display: the video title without a leading show name."""
    return _strip_show_prefix(raw_title, show_name) or raw_title.strip()


def _fit_title(title: str, fixed: str) -> str:
    """Truncates title so that the fixed parts + title fit in MAX_STEM_BYTES (rarely needed:
    YouTube titles are at most 100 characters)."""
    return truncate_utf8(title, max(config.MAX_STEM_BYTES - len(fixed.encode("utf-8")), 10))


def episode_filename(code: str, title: str, video_id: str, ext: str) -> str:
    """'S01E03 - Title [id].mkv'. The show name is left out: the folder and NFO already carry it."""
    prefix, id_part = f"{code} - ", f" [{video_id}]"
    return f"{prefix}{_fit_title(sanitize_filename(title), prefix + id_part)}{id_part}{ext}"


def movie_filename(title: str, video_id: str, ext: str) -> str:
    id_part = f" [{video_id}]"
    return f"{_fit_title(sanitize_filename(title), id_part)}{id_part}{ext}"


def sidecar(video: Path, suffix: str) -> Path:
    return video.with_name(video.stem + suffix)


def _sidecars_of(video: Path) -> list[tuple[Path, str]]:
    """Files that belong to a video (same stem): .nfo, -thumb.jpg, -poster.jpg, -fanart.jpg, .srt."""
    found = []
    stem = video.stem
    for f in video.parent.iterdir():
        if f != video and f.name.startswith(stem):
            rest = f.name[len(stem):]
            if _SIDECAR_RE.fullmatch(rest):
                found.append((f, rest))
    return found


def move_video(src: Path, dst: Path) -> None:
    """Moves/renames a video together with its sidecar files."""
    if src == dst:
        return
    sidecars = _sidecars_of(src) if src.parent.is_dir() else []
    move_file(src, dst)
    for path, suffix in sidecars:
        move_file(path, sidecar(dst, suffix))


def delete_video(path: Path) -> list[str]:
    removed = []
    for f, _ in _sidecars_of(path):
        f.unlink(missing_ok=True)
        removed.append(f.name)
    path.unlink(missing_ok=True)
    removed.append(path.name)
    return removed


# --------------------------------------------------------------------------- planning

def _upload_day(video: LibraryVideo, info: VideoInfo | None) -> str:
    """YYYYMMDD the video was uploaded; falls back to the date already in its name, then file time."""
    if info and info.upload_date:
        return info.upload_date
    m = _DATE_EPISODE_RE.search(video.path.name)
    if m:
        return m.group(1) + m.group(2)
    try:
        return datetime.fromtimestamp(video.path.stat().st_mtime).strftime("%Y%m%d")
    except OSError:
        return datetime.now().strftime("%Y%m%d")


def _playlist_position(video: LibraryVideo | None, info: VideoInfo | None) -> int:
    """Playlist index from the metadata, else the episode number the file already has."""
    if info and info.playlist_index is not None:
        return info.playlist_index
    m = _EPISODE_RE.search(video.path.name) if video else None
    return int(m.group(1)) if m else 999999


def plan_layout(show: Show, videos: list[LibraryVideo], store: MetadataStore) -> list[Placement]:
    """Decides the final path, season and episode number of every identified video."""
    root = show.library_dir
    entries = [(v, store.videos.get(v.id)) for v in videos if v.id]

    def placement(v: LibraryVideo, info: VideoInfo | None, folder: str, code: str, season: int,
                  episode: int) -> Placement:
        title = episode_title(info.title if info and info.title else v.path.stem, show.name)
        name = episode_filename(code, title, v.id, v.path.suffix.lower())
        return Placement(v, info, root / folder / name, title, season, episode)

    if show.is_movie:
        placements = []
        for v, info in entries:
            title = info.title if info and info.title else (_strip_show_prefix(v.path.stem, show.name) or show.name)
            placements.append(Placement(v, info, root / movie_filename(title, v.id, v.path.suffix.lower()), title))
        return placements

    if show.mode == "years":
        days: dict[str, list] = {}
        for v, info in entries:
            days.setdefault(_upload_day(v, info), []).append((v, info))
        placements = []
        for day in sorted(days):
            same_day = sorted(days[day], key=lambda e: ((e[1].timestamp or 0) if e[1] else 0, e[0].id))
            for n, (v, info) in enumerate(same_day, start=1):
                year, month_day = day[:4], day[4:8]
                placements.append(placement(v, info, f"Season {year}", f"S{year}E{month_day}{n:02d}",
                                            int(year), int(f"{month_day}{n:02d}")))
        return placements

    # Episodes follow the playlist order. Videos you deleted keep their number (so later episodes are
    # not renumbered, and Restore puts a video back in its old place); videos the archive never had,
    # such as private ones, take no number.
    present = {v.id for v, _ in entries}
    deleted = [(None, info) for vid, info in store.videos.items()
               if vid not in present and info.playlist_index is not None]
    ordered = sorted(entries + deleted, key=lambda e: (
        _playlist_position(*e), e[1].upload_date if e[1] and e[1].upload_date else "99999999",
        e[0].id if e[0] else e[1].id))
    return [placement(v, info, "Season 01", f"S01E{n:02d}", 1, n)
            for n, (v, info) in enumerate(ordered, start=1) if v]


def _dedupe(videos: list[LibraryVideo], preferred: set[Path]) -> tuple[list[LibraryVideo], list[LibraryVideo]]:
    """Keeps one file per video ID: a freshly downloaded one, else the best container/largest file."""
    by_id: dict[str, list[LibraryVideo]] = {}
    kept, extra = [], []
    for v in videos:
        if v.id:
            by_id.setdefault(v.id, []).append(v)
        else:
            kept.append(v)
    for group in by_id.values():
        def rank(v: LibraryVideo):
            size = v.path.stat().st_size if v.path.exists() else 0
            return (v.path not in preferred, _EXT_PREFERENCE.get(v.path.suffix.lower(), 9), -size)
        group.sort(key=rank)
        kept.append(group[0])
        extra.extend(group[1:])
    return kept, extra


# --------------------------------------------------------------------------- organize

def organize(show: Show, *, incoming: dict[str, Path] | None = None, thumbnails: dict[str, Path] | None = None,
             playlist_art: Path | None = None, refresh_art: bool = False) -> OrganizeResult:
    """Brings a show/movie folder in line with the Kodi layout: names, seasons, NFOs, artwork, junk.

    incoming:     freshly processed videos (id -> temp file inside the library folder) to place.
    thumbnails:   downloaded thumbnails (id -> image) to use as episode/movie artwork.
    playlist_art: downloaded playlist/channel image to use for the show poster.
    refresh_art:  rebuild show posters/fanart (and movie art) even if they already exist.
    Safe to run repeatedly; it only touches files whose name or content is wrong.
    """
    result = OrganizeResult()
    root = show.library_dir
    if not root.is_dir() and not incoming:
        return result
    root.mkdir(parents=True, exist_ok=True)
    store = show.metadata()
    _migrate_legacy_files(show, store, result)

    videos = find_videos(show, store) + [LibraryVideo(p, vid) for vid, p in (incoming or {}).items()]
    videos, duplicates = _dedupe(videos, set((incoming or {}).values()))
    for dup in duplicates:
        log.info("%s: removing duplicate %s", show.name, dup.path.name)
        result.removed += delete_video(dup.path)
    result.unidentified = [v.path.name for v in videos if not v.id]

    placements = plan_layout(show, videos, store)
    for pl in placements:
        if pl.video.path != pl.target:
            if not pl.video.path.name.startswith("."):  # not a new arrival (.tmp-<id>)
                log.log(DETAIL, "%s: renaming %s -> %s", show.name, pl.video.path.name, pl.target.relative_to(root))
                result.renamed += 1
            move_video(pl.video.path, pl.target)
            pl.video.path = pl.target
            result.moved += 1

    for pl in placements:
        if _write_video_nfo(show, pl):
            result.nfos_written += 1
        result.artwork_created += _ensure_video_art(show, pl, (thumbnails or {}).get(pl.video.id), refresh_art)

    if not show.is_movie:
        if write_text_if_changed(root / "tvshow.nfo", nfo.tvshow_nfo(
                show.name, store.playlist, [pl.info for pl in placements if pl.info])):
            result.nfos_written += 1
        channel = (store.playlist.channel if store.playlist else "") or next(
            (pl.info.channel for pl in placements if pl.info and pl.info.channel), "")
        result.artwork_created += _ensure_show_art(show, placements, playlist_art, refresh_art, channel)

    result.removed += _remove_junk(show)
    remove_empty_dirs(root)
    return result


def _write_video_nfo(show: Show, pl: Placement) -> bool:
    if show.is_movie:
        text = nfo.movie_nfo(pl.info, video_id=pl.video.id, title=pl.title)
    else:
        text = nfo.episode_nfo(pl.info, video_id=pl.video.id, title=pl.title, show_title=show.name,
                               season=pl.season, episode=pl.episode)
    return write_text_if_changed(pl.target.with_suffix(".nfo"), text)


def _base_image(video: Path, downloaded: Path | None, dst: Path) -> bool:
    """Best available still for a video: downloaded thumbnail, embedded cover, or a frame grab."""
    if downloaded and downloaded.exists() and media.to_jpeg(downloaded, dst):
        return True
    return media.extract_cover(video, dst) or media.grab_frame(video, dst)


def _ensure_video_art(show: Show, pl: Placement, downloaded: Path | None, refresh: bool) -> int:
    video = pl.target
    if not show.is_movie:
        thumb = sidecar(video, "-thumb.jpg")
        if thumb.exists() and not downloaded:
            return 0
        return int(_base_image(video, downloaded, thumb))

    poster, fanart = sidecar(video, "-poster.jpg"), sidecar(video, "-fanart.jpg")
    existed = poster.exists() and fanart.exists()
    if existed and not downloaded and not refresh:
        return 0
    with tempfile.TemporaryDirectory(prefix="mediasync-") as tmp:
        base = Path(tmp) / "base.jpg"
        if not _base_image(video, downloaded, base):
            return 0
        shutil.copyfile(base, fanart)
        artwork.make_poster(base, poster, pl.title, pl.info.channel if pl.info else "")
    return 0 if existed else 2


def _ensure_show_art(show: Show, placements: list[Placement], playlist_art: Path | None, refresh: bool,
                     channel: str) -> int:
    """poster.jpg (2:3 with title card), fanart.jpg and landscape.jpg (16:9) and one poster per season."""
    root = show.library_dir
    stored = show.metadata_dir / PLAYLIST_ART
    if playlist_art and playlist_art.exists():
        stored.parent.mkdir(parents=True, exist_ok=True)
        media.to_jpeg(playlist_art, stored)

    thumbs = [sidecar(pl.target, "-thumb.jpg") for pl in placements]
    first_thumb = next((t for t in thumbs if t.exists()), None)
    source = stored if stored.exists() else first_thumb
    landscape = stored if stored.exists() and media.image_is_landscape(stored) else first_thumb

    created = 0
    poster = root / "poster.jpg"
    if source and (refresh or not poster.exists()):
        existed = poster.exists()
        if artwork.make_poster(source, poster, show.name, channel) and not existed:
            created += 1
    for name in ("fanart.jpg", "landscape.jpg"):
        if landscape and (refresh or not (root / name).exists()):
            created += not (root / name).exists()
            shutil.copyfile(landscape, root / name)
    return created + _ensure_season_posters(show, placements, refresh)


def _ensure_season_posters(show: Show, placements: list[Placement], refresh: bool) -> int:
    """season01-poster.jpg / season2021-poster.jpg next to the show poster (Kodi's naming)."""
    root = show.library_dir
    seasons: dict[int, list[Placement]] = {}
    for pl in placements:
        seasons.setdefault(pl.season, []).append(pl)
    wanted = {f"season{season:02d}-poster.jpg" for season in seasons}
    for stale in root.glob("season*-poster.jpg"):
        if stale.name not in wanted:
            stale.unlink()

    created = 0
    for season, items in seasons.items():
        target = root / f"season{season:02d}-poster.jpg"
        if target.exists() and not refresh:
            continue
        existed = target.exists()
        if show.mode != "years":  # a playlist is a single season: same poster as the show
            if (root / "poster.jpg").exists():
                shutil.copyfile(root / "poster.jpg", target)
        else:
            thumb = next((t for t in (sidecar(pl.target, "-thumb.jpg") for pl in
                                      sorted(items, key=lambda p: p.episode)) if t.exists()), None)
            if thumb:
                artwork.make_poster(thumb, target, f"Season {season}", show.name)
        created += target.exists() and not existed
    return created


def _migrate_legacy_files(show: Show, store: MetadataStore, result: OrganizeResult) -> None:
    """Older versions left a cache.txt and the playlist thumbnail inside the season folder."""
    root = show.library_dir
    for cache in (root / "cache.txt", show.metadata_dir / "cache.txt"):
        if cache.exists():
            cache.unlink()
            result.removed.append(cache.name)
    if store.playlist:
        for path in list(root.glob("Season */*")):
            if path.suffix.lower() in (".jpg", ".webp", ".png") and f"[{store.playlist.id}]" in path.name:
                stored = show.metadata_dir / PLAYLIST_ART
                if stored.exists() or media.to_jpeg(path, stored):
                    path.unlink()
                    result.removed.append(path.name)


def find_junk(show: Show) -> list[Path]:
    """Download fragments and sidecars whose video no longer exists. Unknown (user) files are never listed."""
    files = list(iter_files(show.library_dir)) if show.library_dir.is_dir() else []
    video_stems: dict[Path, set[str]] = {}
    for f in files:
        if is_video_file(f):
            video_stems.setdefault(f.parent, set()).add(f.stem)
    return [f for f in files if _is_junk(show, f, video_stems.get(f.parent, set()))]


def _remove_junk(show: Show) -> list[str]:
    removed = []
    for path in find_junk(show):
        log.info("%s: removing %s", show.name, path.relative_to(show.library_dir))
        path.unlink(missing_ok=True)
        removed.append(path.name)
    return removed


def _is_junk(show: Show, path: Path, video_stems: set[str]) -> bool:
    name = path.name
    if is_fragment(name):
        return True
    if is_video_file(path):
        return False
    if name == "tvshow.nfo":
        return show.is_movie
    if path.suffix.lower() in (".vtt", ".webp"):
        return video_id_from_name(name) is not None
    if not _SIDECAR_RE.search(name) or (path.parent == show.library_dir and not show.is_movie):
        return False
    return not any(name.startswith(stem) and _SIDECAR_RE.fullmatch(name[len(stem):]) for stem in video_stems)


# --------------------------------------------------------------------------- where things are

@dataclass
class Location:
    show: Show
    status: str     # "library": its folder exists | "new": never downloaded | "offline" | "missing"
    note: str = ""  # why an offline/missing item is left alone


def locate(name: str, mode: str, sub=None, wanted_library: str = "") -> Location:
    """Finds a show/movie: in the first connected library that has its folder (so a folder moved to
    another library is followed), else in the library its subscription names (or wanted_library,
    for something new). Something that was downloaded before but whose folder cannot be found is
    "offline" or "missing" and must not be downloaded again from scratch."""
    is_movie = mode == "movie"
    for lib in config.libraries():
        if lib.online and (lib.path_for(is_movie) / name).is_dir():
            if sub is not None:
                subscriptions.set_location(name, lib.name)
            return Location(Show(name, mode, lib.name), "library")

    target = (config.get_library(wanted_library) or config.get_library(sub.library if sub else "")
              or config.default_library())
    show = Show(name, mode, target.name)
    offline = [lib.name for lib in config.libraries() if not lib.online]
    are = "is" if len(offline) == 1 else "are"
    if not target.online:
        return Location(show, "offline", f"{target.name} is not connected.")
    if sub is not None and not sub.library and offline:  # subscribed before libraries existed: location unknown
        likely = Show(name, mode, offline[0]) if len(offline) == 1 else show
        return Location(likely, "offline", f"Not found; it may be on {_join(offline)}, which {are} not connected.")
    if sub is not None and sub.archived:
        maybe = f" It may be on {_join(offline)}, which {are} not connected." if offline else ""
        return Location(show, "missing", f"Its folder is missing from {target.name}.{maybe}")
    return Location(show, "new")


def move_show(show: Show, target: config.Library, progress: Callable[[int, int], None] | None = None,
              check_cancelled: Callable[[], None] = lambda: None) -> Show:
    """Moves a show/movie folder to another library and returns it in its new place.

    On the same drive this is an instant rename. To another drive, everything is first copied into
    a hidden folder there and checked, and only then is the original deleted: an interruption
    (pause, an unplugged drive, a full disk) leaves the original as it was."""
    show.require_online()
    if not target.online:
        raise LibraryOffline(f"{target.name} is not connected")
    src = show.library_dir
    target_root = target.path_for(show.is_movie)
    target_root.mkdir(parents=True, exist_ok=True)  # the library is connected, so this is its drive
    dst = target_root / show.name
    if dst.exists():
        raise MoveError(f"{target.name} already has a folder called “{show.name}”.")
    moved = Show(show.name, show.mode, target.name)
    if _same_drive(src, target_root):
        src.rename(dst)
        return moved

    files = [p for p in src.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    free = shutil.disk_usage(target_root).free
    if total > free:
        raise MoveError(f"Not enough space on {target.name}: it needs {format_bytes(total)}, "
                        f"and {format_bytes(free)} is free.")
    tmp = target_root / f".moving-{show.name}"
    shutil.rmtree(tmp, ignore_errors=True)  # left by an interrupted move
    done = 0
    try:
        for f in files:
            out = tmp / f.relative_to(src)
            out.parent.mkdir(parents=True, exist_ok=True)
            with open(f, "rb") as fin, open(out, "wb") as fout:
                while chunk := fin.read(8 << 20):
                    check_cancelled()
                    fout.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
            shutil.copystat(f, out)
            if out.stat().st_size != f.stat().st_size:
                raise MoveError(f"Copying {f.relative_to(src)} to {target.name} failed; nothing was moved.")
        tmp.rename(dst)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    shutil.rmtree(src, ignore_errors=True)  # the caller reports it if anything is left
    return moved


def _same_drive(a: Path, b: Path) -> bool:
    return a.stat().st_dev == b.stat().st_dev


def prepare_libraries(old: list[config.Library], new: list[config.Library]) -> list[str]:
    """After the library settings changed: keeps subscriptions pointing at renamed libraries and
    creates the folders of new or moved libraries. Returns the libraries whose folders could not be
    created (a drive that is not connected). Unchanged libraries are left alone, so an unplugged
    drive's folders are never created somewhere else."""
    by_paths = {(lib.shows_dir, lib.movies_dir): lib for lib in old}
    not_created = []
    for lib in new:
        before = by_paths.get((lib.shows_dir, lib.movies_dir))
        if before:
            if before.name != lib.name:
                subscriptions.rename_library(before.name, lib.name)
            continue
        try:
            for folder in (lib.shows_path, lib.movies_path):
                folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            not_created.append(lib.name)
    return not_created


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


# --------------------------------------------------------------------------- catalog for the web UI

def catalog(subs: list) -> list[dict]:
    """Every subscription plus every folder found in a connected library or the staging area."""
    entries: dict[str, dict] = {}
    assigned = folders.all()
    for sub in subs:
        where = locate(sub.name, sub.mode, sub)
        entries[sub.name] = _catalog_entry(where, sub.url, sub.limit, subscribed=True)
    for lib in config.libraries():
        for is_movie in (False, True):
            for folder in _subfolders(lib.path_for(is_movie)) if lib.online else []:
                if folder.name not in entries:
                    show = Show(folder.name, infer_mode(folder, is_movie), lib.name)
                    entries[folder.name] = _catalog_entry(Location(show, "library"), _guess_url(show), None,
                                                          subscribed=False)
    # Anything else in the downloads folder belongs to nothing subscribed or connected.
    offline = [lib.name for lib in config.libraries() if not lib.online]
    for folder in _subfolders(config.STAGING_DIR):
        if folder.name not in entries:
            note = "Files left over from a download that didn't finish; the show itself isn't in a connected library."
            if offline:
                note += f" It may be on {_join(offline)}, which {'is' if len(offline) == 1 else 'are'} not connected."
            show = Show(folder.name, infer_mode(folder, False))
            entries[folder.name] = _catalog_entry(Location(show, "leftovers", note), None, None, subscribed=False)
    for entry in entries.values():
        entry["folder"] = assigned.get(entry["name"], "") if entry["location"] != "leftovers" else ""
    return sorted(entries.values(), key=lambda e: e["name"].lower())


def _subfolders(base: Path) -> list[Path]:
    if not base.is_dir():
        return []
    return [f for f in sorted(base.iterdir()) if f.is_dir() and not f.name.startswith(".")]


def _catalog_entry(where: Location, url: str | None, limit: int | None, subscribed: bool) -> dict:
    show = where.show
    present = where.status == "library"
    files = [f for f in iter_files(show.library_dir) if is_video_file(f)] if present else []
    if where.status == "leftovers":
        files = [f for f in show.staging_dir.rglob("*") if f.is_file()]
    partial = show.staging_dir.is_dir() and any(show.staging_dir.iterdir())
    location = "downloads" if where.status == "new" and partial else where.status
    return {
        "name": show.name,
        "mode": show.mode,
        "library": show.lib.name,
        "url": url,
        "limit": limit,
        "subscribed": subscribed,
        "videos": len(files) if present else 0,
        "deleted": len(deleted_ids(show)) if present and show.metadata_dir.is_dir() else 0,
        "size": sum(_size(f) for f in files),
        "location": location,
        "note": where.note,
        "partial": partial,
    }


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:  # renamed or deleted meanwhile by the processing thread
        return 0


def _guess_url(show: Show) -> str | None:
    """The YouTube address of something not subscribed to, from its saved metadata (no requests)."""
    if not show.metadata_dir.is_dir():
        return None
    store = show.metadata()
    if store.playlist and store.playlist.webpage_url:
        return store.playlist.webpage_url
    if show.is_movie:
        return next((v.webpage_url for v in store.videos.values() if v.webpage_url), None)
    # e.g. from older versions without playlist.info.json: where most of its videos were listed from
    sources = Counter(v.playlist_url for v in store.videos.values() if v.playlist_url)
    return sources.most_common(1)[0][0] if sources else None
