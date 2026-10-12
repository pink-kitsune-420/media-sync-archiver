"""Settings (config.toml in the user's config folder) and fixed constants.

Code reads the active values through this module at call time, e.g. ``config.SHOWS_DIR`` or
``config.settings.max_height``; ``apply()`` swaps them when the settings change.
"""
from __future__ import annotations

import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

APP_NAME = "mediasync"

METADATA_DIR_NAME = ".metadata"
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".webm", ".avi", ".mov", ".m4v"}

# Many file systems (eCryptfs home folders, some network shares) reject names over ~143 bytes.
# Video names (without extension) are capped so even "<name>-fanart.jpg" stays within that.
MAX_STEM_BYTES = 143 - len("-fanart.jpg")

# YouTube needs a JavaScript runtime to unlock all formats; yt-dlp uses whichever is installed.
JS_RUNTIMES = {"deno": {}, "node": {}}

# yt-dlp's own "sleep" preset (yt-dlp -t sleep): pauses that keep the load on YouTube low.
GENTLE_SLEEP = {
    "sleep_interval_requests": 0.75,
    "sleep_interval": 10,
    "max_sleep_interval": 20,
    "sleep_interval_subtitles": 5,
}
MIN_AUTO_UPDATE_HOURS = 6


def _platform_dir(kind: str) -> Path:
    """Standard per-user folders: ~/.config and ~/.cache on Linux, AppData on Windows, Library on macOS."""
    home = os.environ.get("MEDIASYNC_HOME")
    if home:
        return Path(home).expanduser() / kind
    if sys.platform == "win32":
        base = os.environ.get("APPDATA" if kind == "config" else "LOCALAPPDATA") or Path.home() / "AppData"
        return Path(base) / APP_NAME / ("" if kind == "config" else "Cache")
    if sys.platform == "darwin":
        sub = "Application Support" if kind == "config" else "Caches"
        return Path.home() / "Library" / sub / APP_NAME
    xdg = os.environ.get("XDG_CONFIG_HOME" if kind == "config" else "XDG_CACHE_HOME")
    return Path(xdg or Path.home() / (".config" if kind == "config" else ".cache")) / APP_NAME


CONFIG_DIR = _platform_dir("config")
CACHE_DIR = _platform_dir("cache")


@dataclass
class Settings:
    # [library]
    shows_dir: str = "~/Videos/YouTube/Shows"
    movies_dir: str = "~/Videos/YouTube/Movies"
    downloads_dir: str = ""
    # [downloads]
    max_height: int = 1080
    subtitle_languages: list[str] = field(default_factory=lambda: ["en"])
    speed_limit: str = ""
    gentle: bool = True
    # [schedule]
    auto_update_hours: int = 0
    # [web]
    host: str = "127.0.0.1"
    port: int = 8765
    # [kodi]
    kodi_url: str = ""
    kodi_username: str = "kodi"
    kodi_password: str = ""

    def validate(self) -> None:
        """Raises ValueError with a readable message for settings that cannot work."""
        if not self.shows_dir.strip() or not self.movies_dir.strip():
            raise ValueError("The shows and movies folders must be set.")
        if self.max_height not in (360, 480, 720, 1080, 1440, 2160):
            raise ValueError("Maximum quality must be one of 360, 480, 720, 1080, 1440 or 2160.")
        if not self.subtitle_languages or not all(isinstance(x, str) and x for x in self.subtitle_languages):
            raise ValueError("Give at least one subtitle language code, e.g. en.")
        if self.auto_update_hours and self.auto_update_hours < MIN_AUTO_UPDATE_HOURS:
            raise ValueError(f"Automatic updates can run at most every {MIN_AUTO_UPDATE_HOURS} hours.")
        if not 1 <= self.port <= 65535:
            raise ValueError("The port must be between 1 and 65535.")


# section -> fields, also the order used when writing config.toml
_SECTIONS = {
    "library": ("shows_dir", "movies_dir", "downloads_dir"),
    "downloads": ("max_height", "subtitle_languages", "speed_limit", "gentle"),
    "schedule": ("auto_update_hours",),
    "web": ("host", "port"),
    "kodi": ("kodi_url", "kodi_username", "kodi_password"),
}
_COMMENTS = {
    "shows_dir": "Kodi 'TV shows' source: channels and playlists",
    "movies_dir": "Kodi 'Movies' source: single videos",
    "downloads_dir": "where unfinished downloads wait (empty = the app's cache folder)",
    "max_height": "highest video quality to download: 720, 1080, 1440 or 2160",
    "subtitle_languages": "subtitle languages to embed when YouTube has them",
    "speed_limit": "e.g. \"2M\" for 2 MB/s; empty = unlimited",
    "gentle": "pause between requests and downloads like yt-dlp's 'sleep' preset (recommended)",
    "auto_update_hours": f"check subscriptions every N hours (0 = only when you ask; minimum {MIN_AUTO_UPDATE_HOURS})",
    "host": "127.0.0.1 = this computer only; 0.0.0.0 = any device on your network",
    "kodi_url": "optional, e.g. http://localhost:8080/jsonrpc to make Kodi rescan after downloads",
}


def config_file() -> Path:
    return CONFIG_DIR / "config.toml"


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def to_toml(settings: Settings) -> str:
    lines = ["# Media Sync Archiver settings. Edit here or on the dashboard's Settings page.", ""]
    values = asdict(settings)
    for section, names in _SECTIONS.items():
        lines.append(f"[{section}]")
        for name in names:
            if name in _COMMENTS:
                lines.append(f"# {_COMMENTS[name]}")
            lines.append(f"{name} = {_toml_value(values[name])}")
        lines.append("")
    return "\n".join(lines)


def from_dict(data: dict) -> Settings:
    """Builds Settings from a flat or sectioned dict, ignoring unknown keys."""
    flat = {}
    for key, value in data.items():
        if isinstance(value, dict):
            flat.update(value)
        else:
            flat[key] = value
    known = {f.name: f for f in fields(Settings)}
    kwargs = {}
    for key, value in flat.items():
        if key not in known:
            continue
        default = getattr(Settings(), key)
        if isinstance(default, bool):
            value = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
        elif isinstance(default, int):
            value = int(value)
        elif isinstance(default, list):
            value = [v.strip() for v in (value.split(",") if isinstance(value, str) else value) if str(v).strip()]
        else:
            value = str(value).strip()
        kwargs[key] = value
    return Settings(**kwargs)


def load() -> Settings:
    """Reads config.toml, creating it with defaults on first run."""
    path = config_file()
    if not path.exists():
        settings = Settings()
        save(settings)
        return settings
    with open(path, "rb") as f:
        return from_dict(tomllib.load(f))


def save(settings: Settings) -> None:
    from .util import write_atomic  # local import: util has no dependency on config
    settings.validate()
    write_atomic(config_file(), to_toml(settings))


# ----------------------------------------------------------------- active values

settings = Settings()
SUBSCRIPTIONS_FILE = CONFIG_DIR / "subscriptions.json"
STATE_FILE = CONFIG_DIR / "state.json"
SHOWS_DIR = Path()
MOVIES_DIR = Path()
STAGING_DIR = Path()


def apply(new: Settings) -> None:
    """Makes new the active settings (paths are expanded here once)."""
    global settings, SHOWS_DIR, MOVIES_DIR, STAGING_DIR
    settings = new
    SHOWS_DIR = Path(new.shows_dir).expanduser()
    MOVIES_DIR = Path(new.movies_dir).expanduser()
    STAGING_DIR = Path(new.downloads_dir).expanduser() if new.downloads_dir else CACHE_DIR / "downloads"


def video_format() -> str:
    h = settings.max_height
    return f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"


apply(settings)
