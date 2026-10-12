"""ffprobe/ffmpeg helpers: inspecting videos, embedding subs/chapters/tags in one pass, and artwork."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from .metadata import VideoInfo

# ISO 639-1 -> 639-2 for the subtitle language tag (Kodi shows "English" instead of "Unknown").
_LANG_CODES = {"en": "eng", "de": "ger", "fr": "fre", "es": "spa", "it": "ita", "ja": "jpn", "pt": "por"}


class MediaError(Exception):
    pass


def _run(cmd: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)


@dataclass(frozen=True)
class Probe:
    readable: bool
    error: str = ""
    duration: float = 0.0
    video_streams: int = 0          # excluding embedded cover art
    audio_streams: int = 0
    subtitle_streams: int = 0
    chapters: int = 0
    has_title_tag: bool = False
    mapped_streams: int = 0         # streams kept when remuxing (everything except data tracks)
    cover_index: int | None = None  # stream index of the embedded cover image
    cover_is_attachment: bool = False
    width: int = 0
    height: int = 0


_probe_cache: dict[str, tuple[int, int, Probe]] = {}
_verify_cache: dict[str, tuple[int, int, str | None]] = {}
_cache_lock = threading.Lock()


def _file_key(path: Path) -> tuple[str, int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return str(path), st.st_mtime_ns, st.st_size


def probe(path: Path) -> Probe:
    key = _file_key(path)
    if key is None:
        return Probe(readable=False, error="file not found")
    with _cache_lock:
        cached = _probe_cache.get(key[0])
        if cached and cached[:2] == key[1:]:
            return cached[2]
    result = _probe_uncached(path) if key[2] > 0 else Probe(readable=False, error="empty file")
    with _cache_lock:
        _probe_cache[key[0]] = (key[1], key[2], result)
    return result


def _to_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _probe_uncached(path: Path) -> Probe:
    res = _run(["ffprobe", "-v", "error", "-show_entries",
                "format=duration:format_tags:stream=index,codec_type,width,height"
                ":stream_disposition=attached_pic:stream_tags=mimetype:chapter=id",
                "-of", "json", str(path)], timeout=120)
    try:
        data = json.loads(res.stdout or "{}")
    except ValueError:
        data = {}
    if res.returncode != 0 or "format" not in data:
        return Probe(readable=False, error=(res.stderr.strip().splitlines() or ["unreadable"])[-1])

    streams = data.get("streams", [])
    video = audio = subs = mapped = 0
    cover_index, cover_is_attachment = None, False
    width = height = 0
    for s in streams:
        kind = s.get("codec_type")
        mimetype = (s.get("tags") or {}).get("mimetype", "")
        if kind == "video" and s.get("disposition", {}).get("attached_pic"):
            cover_index = s["index"]
        elif kind == "attachment" and mimetype.startswith("image/"):
            cover_index, cover_is_attachment = s["index"], True
        elif kind == "video":
            video += 1
            width, height = width or s.get("width", 0), height or s.get("height", 0)
        elif kind == "audio":
            audio += 1
        elif kind == "subtitle":
            subs += 1
        if kind != "data":
            mapped += 1
    tags = {k.lower(): v for k, v in (data["format"].get("tags") or {}).items()}
    return Probe(
        readable=True,
        duration=_to_float(data["format"].get("duration")),
        video_streams=video,
        audio_streams=audio,
        subtitle_streams=subs,
        chapters=len(data.get("chapters", [])),
        has_title_tag=bool(tags.get("title")),
        mapped_streams=mapped,
        cover_index=cover_index,
        cover_is_attachment=cover_is_attachment,
        width=width,
        height=height,
    )


def find_problem(path: Path, info: VideoInfo | None) -> str | None:
    """Quick check (ffprobe only): returns why the file is unusable, or None when it looks fine."""
    p = probe(path)
    if not p.readable:
        return f"unreadable ({p.error})"
    if not p.video_streams:
        return "no video stream"
    if not p.audio_streams and (info is None or info.has_audio):
        return "no audio stream"
    return None


def verify(path: Path, info: VideoInfo | None) -> str | None:
    """Thorough check: reads the whole file (no decoding) to catch truncated or corrupt downloads."""
    problem = find_problem(path, info)
    if problem:
        return problem
    key = _file_key(path)
    with _cache_lock:
        cached = _verify_cache.get(key[0]) if key else None
        if cached and key and cached[:2] == key[1:]:
            return cached[2]

    res = _run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", "0:V:0", "-map", "0:a:0?",
                "-c", "copy", "-f", "null", "-", "-progress", "pipe:1", "-nostats"])
    last_us = 0
    for line in res.stdout.splitlines():
        if line.startswith("out_time_us="):
            try:
                last_us = max(last_us, int(line.split("=", 1)[1]))
            except ValueError:
                pass
    played = last_us / 1_000_000
    expected = (info.duration if info and info.duration else 0) or probe(path).duration
    problem = None
    errors = res.stderr.strip()
    if res.returncode != 0:
        problem = f"cannot be read ({(errors.splitlines() or ['ffmpeg error'])[-1]})"
    elif expected and played < expected - max(5.0, expected * 0.02):
        problem = f"truncated ({played:.0f}s of {expected:.0f}s)"
    elif "ended prematurely" in errors or "Invalid data" in errors:
        problem = f"corrupt ({errors.splitlines()[-1]})"
    if key:
        with _cache_lock:
            _verify_cache[key[0]] = (key[1], key[2], problem)
    return problem


def _escape_ffmetadata(value: str) -> str:
    for ch in ("\\", "=", ";", "#", "\n"):
        value = value.replace(ch, "\\" + ch)
    return value


def build_ffmetadata(info: VideoInfo) -> str:
    """Global tags and chapters in ffmpeg's FFMETADATA format."""
    lines = [";FFMETADATA1"]
    tags = {
        "title": info.title,
        "artist": info.channel,
        "date": info.upload_date,
        "description": info.description,
        "comment": info.webpage_url,
    }
    lines += [f"{key}={_escape_ffmetadata(value)}" for key, value in tags.items() if value]
    for i, chapter in enumerate(info.chapters):
        start = float(chapter["start_time"])
        end = chapter.get("end_time")
        if end is None:
            end = info.chapters[i + 1]["start_time"] if i + 1 < len(info.chapters) else (info.duration or start)
        lines += ["", "[CHAPTER]", "TIMEBASE=1/1000", f"START={int(start * 1000)}", f"END={int(float(end) * 1000)}",
                  f"title={_escape_ffmetadata(chapter.get('title') or f'Chapter {i + 1}')}"]
    return "\n".join(lines) + "\n"


def remux(src: Path, dst: Path, *, info: VideoInfo | None = None, subtitle: Path | None = None,
          subtitle_lang: str = "en", cover: Path | None = None) -> None:
    """Copies all streams of src into dst (.mkv or .mp4) while adding tags/chapters, a subtitle
    track and cover art in a single pass. Nothing is re-encoded. Raises MediaError on failure."""
    src_probe = probe(src)
    if not src_probe.readable:
        raise MediaError(f"cannot read {src.name}: {src_probe.error}")
    is_mp4 = dst.suffix.lower() == ".mp4"
    with tempfile.TemporaryDirectory(prefix="mediasync-") as tmp:
        cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(src)]
        next_input = 1
        sub_input = meta_input = cover_input = None
        if subtitle:
            cmd += ["-i", str(subtitle)]
            sub_input, next_input = next_input, next_input + 1
        if info:
            meta_file = Path(tmp) / "metadata.txt"
            meta_file.write_text(build_ffmetadata(info), encoding="utf-8")
            cmd += ["-f", "ffmetadata", "-i", str(meta_file)]
            meta_input, next_input = next_input, next_input + 1
        if cover and is_mp4:
            cmd += ["-i", str(cover)]
            cover_input = next_input

        # Data tracks (e.g. mp4 chapter text) are dropped; chapters are re-added from the metadata.
        cmd += ["-map", "0", "-map", "-0:d?"]
        out_index = src_probe.mapped_streams
        if sub_input is not None:
            cmd += ["-map", str(sub_input)]
        if cover_input is not None:
            cmd += ["-map", str(cover_input)]
        if meta_input is not None:
            cmd += ["-map_metadata", str(meta_input)]
            if info.chapters:
                cmd += ["-map_chapters", str(meta_input)]
        cmd += ["-c", "copy"]
        if sub_input is not None:
            if is_mp4:
                cmd += [f"-c:{out_index}", "mov_text"]
            cmd += [f"-metadata:s:{out_index}", f"language={_LANG_CODES.get(subtitle_lang, subtitle_lang)}"]
            out_index += 1
        if cover_input is not None:
            cmd += [f"-disposition:{out_index}", "attached_pic"]
        elif cover:
            cmd += ["-attach", str(cover), f"-metadata:s:{out_index}", "mimetype=image/jpeg",
                    f"-metadata:s:{out_index}", "filename=cover.jpg"]
        cmd.append(str(dst))

        res = _run(cmd)
    if res.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        dst.unlink(missing_ok=True)
        raise MediaError(f"ffmpeg failed for {src.name}: {(res.stderr.strip().splitlines() or ['unknown error'])[-1]}")


def to_jpeg(src: Path, dst: Path) -> bool:
    """Copies or converts an image (jpg/webp/png) to a jpg."""
    if src.suffix.lower() in (".jpg", ".jpeg"):
        shutil.copyfile(src, dst)
        return True
    res = _run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(src), "-frames:v", "1", "-q:v", "2", str(dst)])
    return res.returncode == 0 and dst.exists() and dst.stat().st_size > 0


def extract_cover(video: Path, dst: Path) -> bool:
    """Saves the cover art embedded in a video as a jpg. Returns False when there is none."""
    p = probe(video)
    if p.cover_index is None:
        return False
    with tempfile.TemporaryDirectory(prefix="mediasync-") as tmp:
        if p.cover_is_attachment:
            raw = Path(tmp) / "cover.img"
            _run(["ffmpeg", "-nostdin", "-v", "error", "-y", f"-dump_attachment:{p.cover_index}", str(raw),
                  "-i", str(video), "-t", "0", "-f", "null", "-"])
            return raw.exists() and to_jpeg(raw, dst)
        res = _run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(video), "-map", f"0:{p.cover_index}",
                    "-frames:v", "1", "-q:v", "2", str(dst)])
        return res.returncode == 0 and dst.exists()


def grab_frame(video: Path, dst: Path) -> bool:
    """Fallback artwork: a frame from 10% into the video."""
    duration = probe(video).duration
    res = _run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{duration * 0.1:.2f}", "-i", str(video),
                "-frames:v", "1", "-q:v", "2", str(dst)])
    return res.returncode == 0 and dst.exists()


def image_is_landscape(path: Path) -> bool:
    p = probe(path)
    return p.width > p.height if p.readable else False
