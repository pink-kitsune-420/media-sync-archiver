"""The subscription list (subscriptions.json): every channel, playlist or video the app keeps in sync."""
from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, replace

from . import config
from .util import read_json, write_json

MODES = ("playlist", "years", "movie")


@dataclass
class Subscription:
    name: str
    url: str
    # "playlist": one season in playlist order, "years": one season per upload year, "movie": single video
    mode: str = "playlist"
    limit: int | None = None
    library: str = ""        # the library it lives in (or will be downloaded to); "" = the default one
    archived: bool = False   # it has been downloaded: a missing folder then never means "download it all"

    @property
    def is_movie(self) -> bool:
        return self.mode == "movie"


def normalize_mode(mode: str | None, default: str = "playlist") -> str:
    return mode if mode in MODES else default


def normalize_limit(limit) -> int | None:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _same_url(a: str, b: str) -> bool:
    return a.strip().rstrip("/") == b.strip().rstrip("/")


class SubscriptionStore:
    """Thread-safe access to subscriptions.json. Re-read on every call so manual edits are picked up."""

    def __init__(self, path=None):
        self._path = path
        self._lock = threading.RLock()

    @property
    def path(self):
        return self._path or config.SUBSCRIPTIONS_FILE

    def all(self) -> list[Subscription]:
        with self._lock:
            data = read_json(self.path) or {}
            subs = []
            for entry in data.get("subscriptions", data.get("channels", [])):  # "channels": older versions
                if entry.get("name") and entry.get("url"):
                    subs.append(Subscription(
                        name=entry["name"],
                        url=entry["url"],
                        mode=normalize_mode(entry.get("mode")),
                        limit=normalize_limit(entry.get("limit")),
                        library=str(entry.get("library") or ""),
                        # version 1.0 subscribed when a download started, so its entries were downloaded
                        archived=bool(entry.get("archived", "library" not in entry)),
                    ))
            return subs

    def get(self, name: str) -> Subscription | None:
        return next((s for s in self.all() if s.name == name), None)

    def find_by_url(self, url: str, mode: str | None = None) -> Subscription | None:
        return next((s for s in self.all() if _same_url(s.url, url) and (mode is None or s.mode == mode)), None)

    def upsert(self, sub: Subscription) -> None:
        with self._lock:
            subs = self.all()
            for i, existing in enumerate(subs):
                if existing.name == sub.name:
                    if existing == sub:
                        return
                    subs[i] = sub
                    break
            else:
                subs.append(sub)
            self._save(subs)

    def set_location(self, name: str, library: str) -> None:
        """Records that a subscription's files are (now) in this library."""
        with self._lock:
            sub = self.get(name)
            if sub and (sub.library, sub.archived) != (library, True):
                self.upsert(replace(sub, library=library, archived=True))

    def rename_library(self, old: str, new: str) -> None:
        with self._lock:
            subs = self.all()
            if any(s.library == old for s in subs):
                self._save([replace(s, library=new) if s.library == old else s for s in subs])

    def remove(self, name: str) -> bool:
        with self._lock:
            subs = self.all()
            kept = [s for s in subs if s.name != name]
            if len(kept) == len(subs):
                return False
            self._save(kept)
            return True

    def _save(self, subs: list[Subscription]) -> None:
        write_json(self.path, {"subscriptions": [asdict(s) for s in subs]}, indent=4)


subscriptions = SubscriptionStore()
