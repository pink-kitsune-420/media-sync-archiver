"""Small helpers shared across the package: file names, video IDs and safe file writes."""
from __future__ import annotations

import errno
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

VIDEO_ID_PATTERN = r"[A-Za-z0-9_-]{11}"
_BRACKETED_ID_RE = re.compile(rf"\[({VIDEO_ID_PATTERN})\]")
_BARE_ID_RE = re.compile(rf"^{VIDEO_ID_PATTERN}$")
_INVALID_FILENAME_CHARS_RE = re.compile(r'[\\/*?:"<>|：？＂＊｜＜＞／＼]')
# yt-dlp intermediates: "x.f137.mp4", "x.temp.mkv", "x.mkv.part", "x.mkv.ytdl", "x.part-Frag12"
_FRAGMENT_RE = re.compile(r"(\.f\d+\.\w+|\.temp\.\w+|\.part|\.ytdl|\.part-Frag\d+)$", re.IGNORECASE)


def sanitize_filename(name: str) -> str:
    """Removes characters that are invalid in file names and collapses whitespace."""
    return re.sub(r"\s+", " ", _INVALID_FILENAME_CHARS_RE.sub("", name)).strip()


def truncate_utf8(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip()


def video_id_from_name(name: str) -> str | None:
    """Finds the YouTube ID in 'Title [id].mkv' (library) or 'id.mkv' (staging) style names."""
    matches = _BRACKETED_ID_RE.findall(name)
    if matches:
        return matches[-1]
    head = name.split(".", 1)[0]
    return head if _BARE_ID_RE.match(head) else None


def is_fragment(name: str) -> bool:
    """True for partial/intermediate yt-dlp files that must never be treated as finished videos."""
    return bool(_FRAGMENT_RE.search(name))


def clean_text_for_xml(text: str | None) -> str:
    """Drops characters that are invalid in XML, and emoji/other 4-byte characters, which some Kodi
    setups cannot store; normalises non-breaking spaces."""
    if not text:
        return ""
    cleaned = text.replace("\xa0", " ")
    return "".join(ch for ch in cleaned if (ord(ch) >= 32 or ch in "\n\r\t") and ord(ch) <= 0xFFFF)


def read_json(path: Path) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def write_atomic(path: Path, data: str | bytes) -> None:
    """Writes via a temp file + rename so readers never see a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, mode, **({} if mode == "wb" else {"encoding": "utf-8"})) as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_json(path: Path, data: dict, indent: int | None = None) -> None:
    write_atomic(path, json.dumps(data, ensure_ascii=False, indent=indent))


def write_text_if_changed(path: Path, text: str) -> bool:
    """Writes the file only when its content differs. Returns True if it was written."""
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except (OSError, UnicodeDecodeError):
        pass
    write_atomic(path, text)
    return True


def move_file(src: Path, dst: Path) -> None:
    """Moves a file, replacing dst. Falls back to copy+delete across filesystems."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dst)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        shutil.move(str(src), str(dst))


def remove_empty_dirs(root: Path) -> None:
    """Removes empty sub-directories of root (deepest first), never root itself."""
    if not root.is_dir():
        return
    for path in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            path.rmdir()
        except OSError:
            pass


def format_bytes(num: float | None) -> str:
    if not num:
        return "0 B"
    for unit in ("B", "KiB", "MiB", "GiB"):
        if num < 1024 or unit == "GiB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{num:.0f} B"
        num /= 1024
    return f"{num:.1f} GiB"


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
