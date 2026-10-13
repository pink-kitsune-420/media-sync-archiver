"""Folders in the dashboard's library list (folders.json next to the settings).

They only organize the list: nothing on disk moves, and Kodi is not affected. A folder stays until
it is removed, even while it is empty.
"""
from __future__ import annotations

import threading

from . import config
from .util import read_json, write_json

MAX_NAME = 60


def clean_name(name) -> str:
    return " ".join(str(name or "").split())[:MAX_NAME]


class FolderStore:
    """Which folder each show/movie is in (by name), and which folders exist. Re-read on every call
    so manual edits are picked up."""

    def __init__(self, path=None):
        self._path = path
        self._lock = threading.RLock()

    @property
    def path(self):
        return self._path or config.FOLDERS_FILE

    def _read(self) -> tuple[dict[str, str], set[str]]:
        data = read_json(self.path) or {}
        assigned = {str(k): clean_name(v) for k, v in (data.get("folders") or {}).items() if clean_name(v)}
        names = {clean_name(n) for n in data.get("names") or [] if clean_name(n)}
        return assigned, names | set(assigned.values())

    def all(self) -> dict[str, str]:
        with self._lock:
            return self._read()[0]

    def names(self) -> list[str]:
        """Every folder, including empty ones."""
        with self._lock:
            return sorted(self._read()[1], key=str.casefold)

    def create(self, folder: str) -> None:
        with self._lock:
            assigned, names = self._read()
            self._save(assigned, names | {clean_name(folder)} - {""})

    def put(self, name: str, folder: str) -> None:
        """Puts a show/movie in a folder ("" takes it out of its folder; the folder stays)."""
        with self._lock:
            assigned, names = self._read()
            folder = clean_name(folder)
            if folder:
                assigned[name] = folder
                names.add(folder)
            else:
                assigned.pop(name, None)
            self._save(assigned, names)

    def rename(self, old: str, new: str) -> int:
        """Renames a folder ("" removes it; what was in it stays in the library). Returns how many moved."""
        with self._lock:
            assigned, names = self._read()
            new = clean_name(new)
            moved = [name for name, folder in assigned.items() if folder == old]
            for name in moved:
                if new:
                    assigned[name] = new
                else:
                    del assigned[name]
            names.discard(old)
            if new:
                names.add(new)
            self._save(assigned, names)
            return len(moved)

    def _save(self, assigned: dict[str, str], names: set[str]) -> None:
        write_json(self.path, {"names": sorted(names, key=str.casefold), "folders": dict(sorted(assigned.items()))},
                   indent=4)


folders = FolderStore()
