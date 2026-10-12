"""Folders in the dashboard's library list (folders.json next to the settings).

They only organize the list: nothing on disk moves, and Kodi is not affected. A folder exists while
something is in it.
"""
from __future__ import annotations

import threading

from . import config
from .util import read_json, write_json

MAX_NAME = 60


def clean_name(name) -> str:
    return " ".join(str(name or "").split())[:MAX_NAME]


class FolderStore:
    """Which folder each show/movie is in, by name. Re-read on every call so manual edits are picked up."""

    def __init__(self, path=None):
        self._path = path
        self._lock = threading.RLock()

    @property
    def path(self):
        return self._path or config.FOLDERS_FILE

    def all(self) -> dict[str, str]:
        with self._lock:
            data = (read_json(self.path) or {}).get("folders") or {}
            return {str(k): clean_name(v) for k, v in data.items() if clean_name(v)}

    def put(self, name: str, folder: str) -> None:
        """Puts a show/movie in a folder ("" takes it out of its folder)."""
        with self._lock:
            assigned = self.all()
            folder = clean_name(folder)
            if folder:
                assigned[name] = folder
            else:
                assigned.pop(name, None)
            self._save(assigned)

    def rename(self, old: str, new: str) -> int:
        """Renames a folder ("" removes it; what was in it stays in the library). Returns how many moved."""
        with self._lock:
            assigned = self.all()
            new = clean_name(new)
            names = [name for name, folder in assigned.items() if folder == old]
            for name in names:
                if new:
                    assigned[name] = new
                else:
                    del assigned[name]
            self._save(assigned)
            return len(names)

    def _save(self, assigned: dict[str, str]) -> None:
        write_json(self.path, {"folders": dict(sorted(assigned.items()))}, indent=4)


folders = FolderStore()
