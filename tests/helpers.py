"""Test helpers: a throw-away library, synthetic videos made with ffmpeg and fake info.json files."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from mediasync import config

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not installed")


_PATCHED = ("settings", "CONFIG_DIR", "SUBSCRIPTIONS_FILE", "STATE_FILE", "LOG_DIR", "STAGING_DIR", "SHOWS_DIR",
            "MOVIES_DIR")


class TempLibraryTestCase(unittest.TestCase):
    """Points the config (settings, subscriptions, library, staging) at a temporary folder."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="mediasync-test-")
        self.root = Path(self._tmp.name)
        self._saved = {k: getattr(config, k) for k in _PATCHED}
        config.CONFIG_DIR = self.root / "config"
        config.SUBSCRIPTIONS_FILE = config.CONFIG_DIR / "subscriptions.json"
        config.STATE_FILE = config.CONFIG_DIR / "state.json"
        config.LOG_DIR = config.CONFIG_DIR / "logs"
        config.apply(config.Settings(shows_dir=str(self.root / "Videos" / "Shows"),
                                     movies_dir=str(self.root / "Videos" / "Movies"),
                                     downloads_dir=str(self.root / "downloads"), gentle=False))
        for d in (config.STAGING_DIR, config.SHOWS_DIR, config.MOVIES_DIR):
            d.mkdir(parents=True)

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(config, k, v)
        self._tmp.cleanup()


def make_video(path: Path, seconds: float = 3, audio: bool = True, container_args=()) -> Path:
    """A tiny real video (h264 + optional aac) so ffprobe/ffmpeg behave as with real downloads."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=160x90:rate=10:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", *container_args, str(path)]
    subprocess.run(cmd, check=True)
    return path


def make_image(path: Path, size: str = "320x180") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}",
                    "-frames:v", "1", str(path)], check=True)
    return path


def make_srt(path: Path) -> Path:
    path.write_text("1\n00:00:00,500 --> 00:00:01,500\nHello\n", encoding="utf-8")
    return path


def info(video_id: str, title: str, upload_date: str = "20210404", index: int | None = None, **extra) -> dict:
    data = {
        "id": video_id, "title": title, "upload_date": upload_date, "playlist_index": index,
        "description": f"About {title}", "duration": 3, "channel": "Test Channel", "acodec": "aac",
        "webpage_url": f"https://www.youtube.com/watch?v={video_id}", "automatic_captions": {"en": []},
        "_type": "video",
    }
    data.update(extra)
    return data


def write_info(directory: Path, data: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{data['id']}.info.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def placeholder(path: Path, size: int = 1000) -> Path:
    """An empty stand-in for a video when only names matter."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.truncate(size)
    return path
