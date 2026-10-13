"""Kodi .nfo documents for episodes, movies and TV shows."""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET

from .metadata import PlaylistInfo, VideoInfo
from .util import clean_text_for_xml

_NO_PLOT = "No description available."


def _render(root: ET.Element) -> str:
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def _add(parent: ET.Element, tag: str, value, **attrs) -> None:
    if value is None or value == "":
        return
    ET.SubElement(parent, tag, attrs).text = clean_text_for_xml(str(value))


def _runtime_minutes(info: VideoInfo | None) -> int | None:
    return max(1, round(info.duration / 60)) if info and info.duration else None


def episode_nfo(info: VideoInfo | None, *, video_id: str, title: str, show_title: str,
                season: int, episode: int) -> str:
    root = ET.Element("episodedetails")
    _add(root, "title", title)
    _add(root, "showtitle", show_title)
    _add(root, "season", season)
    _add(root, "episode", episode)
    if info:
        _add(root, "aired", info.aired)
        _add(root, "premiered", info.aired)
        _add(root, "runtime", _runtime_minutes(info))
        _add(root, "studio", info.channel)
    _add(root, "plot", (info.description if info else "") or _NO_PLOT)
    _add(root, "uniqueid", video_id, type="youtube", default="true")
    return _render(root)


def movie_nfo(info: VideoInfo | None, *, video_id: str, title: str) -> str:
    root = ET.Element("movie")
    _add(root, "title", title)
    if info:
        _add(root, "premiered", info.aired)
        _add(root, "year", info.year)
        _add(root, "runtime", _runtime_minutes(info))
        _add(root, "studio", info.channel)
        for genre in sorted(set(info.categories)):
            _add(root, "genre", genre)
        for tag in pick_tags([info], title, info.channel, limit=8):
            _add(root, "tag", tag)
    _add(root, "plot", (info.description if info else "") or _NO_PLOT)
    _add(root, "uniqueid", video_id, type="youtube", default="true")
    return _render(root)


def tvshow_nfo(title: str, playlist: PlaylistInfo | None, videos: list[VideoInfo]) -> str:
    channel = (playlist.channel if playlist else "") or next((v.channel for v in videos if v.channel), "")
    aired = sorted(v.aired for v in videos if v.aired)
    description = (playlist.description if playlist else "").strip()

    root = ET.Element("tvshow")
    _add(root, "title", title)
    _add(root, "plot", description or f"YouTube videos from {channel or title}.")
    _add(root, "premiered", aired[0] if aired else "")
    _add(root, "studio", channel)
    for genre in sorted({c for v in videos for c in v.categories}):
        _add(root, "genre", genre)
    for tag in pick_tags(videos, title, channel):
        _add(root, "tag", tag)
    if playlist:
        _add(root, "uniqueid", playlist.id, type="youtube", default="true")
    return _render(root)


# "#offgrid" or "(#offgrid)", but not the "#Section" at the end of a link
_HASHTAG_RE = re.compile(r"(?<![^\s(\[])#(\w+)")
_GENERIC_TAGS = {"video", "videos", "youtube", "shorts"}


def _hashtags(video: VideoInfo) -> list[str]:
    """Hashtags in the title and description: many creators use these instead of YouTube's tags."""
    found = _HASHTAG_RE.findall(f"{video.title}\n{video.description}")[:10]
    return [re.sub(r"(?<=[a-z])(?=[A-Z])", " ", tag.replace("_", " ")) for tag in found]  # CabinLife -> Cabin Life


def _squash(text: str) -> str:
    """"Clay Corner", "#ClayCorner" and "clay_corner" all become "claycorner"."""
    return re.sub(r"[\W_]+", "", text.lower())


def _singular(word: str) -> str:
    """So that "glaze" and "glazes" count as one tag (but "class", "news", "campus" stay as they are)."""
    plural = len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is", "ews"))
    return word[:-1] if plural else word


def _display(tag: str) -> str:
    """How a tag is shown: "log cabin" -> "Log Cabin", "WOODWORKING" -> "Woodworking"; "DIY", "iPhone" stay."""
    words = tag.split(" ")
    if tag.islower() or (tag.isupper() and len(_squash(tag)) > 4):
        return " ".join(w[:1].upper() + w[1:].lower() for w in words)
    return tag


def pick_tags(videos: list[VideoInfo], show_name: str, channel: str, limit: int = 10) -> list[str]:
    """The most relevant tags (YouTube tags, then hashtags): those shared by the most videos, then
    those their creator listed first (the most important ones usually come first). In a show of
    three or more videos a tag must be on at least two of them (and on a tenth), so one episode's
    topic doesn't become a tag of the whole show. Tags that are just the show's or the channel's
    name are left out."""
    ignore = {_squash(show_name), _squash(channel), *_GENERIC_TAGS}
    counts: dict[str, int] = {}
    positions: dict[str, list[int]] = {}
    spelling: dict[str, str] = {}
    for video in videos:
        seen = set()
        for position, raw in enumerate([*video.tags, *_hashtags(video)]):
            tag = " ".join(raw.strip().lstrip("#").split())
            key = " ".join(_singular(w) for w in tag.lower().split())
            if len(tag) <= 2 or len(tag.split()) > 3 or key in seen or _squash(tag) in ignore:
                continue
            seen.add(key)
            counts[key] = counts.get(key, 0) + 1
            positions.setdefault(key, []).append(position)
            spelling.setdefault(key, tag)
    needed = 1 if len(videos) < 3 else max(2, math.ceil(len(videos) / 10))
    ranked = sorted((k for k in counts if counts[k] >= needed),
                    key=lambda k: (-counts[k], sum(positions[k]) / len(positions[k])))
    return [_display(spelling[k]) for k in ranked[:limit]]
