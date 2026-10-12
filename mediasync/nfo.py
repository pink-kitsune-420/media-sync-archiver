"""Kodi .nfo documents for episodes, movies and TV shows."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import Counter

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
    for tag in _pick_tags(videos, title, channel):
        _add(root, "tag", tag)
    if playlist:
        _add(root, "uniqueid", playlist.id, type="youtube", default="true")
    return _render(root)


def _pick_tags(videos: list[VideoInfo], show_name: str, channel: str, limit: int = 15) -> list[str]:
    """The most common short video tags, minus words that just repeat the show/channel name."""
    show_words = set(re.findall(r"\w+", show_name.lower()))
    ignore = show_words | set(re.findall(r"\w+", channel.lower())) | {"video", "youtube"}
    ignore.add("".join(w[0] for w in show_name.split() if w).lower())

    counts = Counter(tag.strip() for v in videos for tag in set(v.tags) if tag.strip())
    picked, seen = [], set()
    for tag, _ in counts.most_common():
        low = tag.lower()
        singular = low[:-1] if low.endswith("s") else low
        if len(low) <= 2 or low in ignore or len(tag.split()) > 2 or singular in seen or low + "s" in seen:
            continue
        seen.add(low)
        seen.add(singular)
        picked.append(tag.title() if tag.islower() or tag.isupper() else tag)
        if len(picked) == limit:
            break
    return sorted(picked)
