"""Tests never touch the real settings, cache or library: before the app is imported, everything
is pointed at a throw-away folder (individual tests then use their own temporary folders)."""
import atexit
import os
import shutil
import tempfile

_HOME = tempfile.mkdtemp(prefix="mediasync-tests-")
os.environ["MEDIASYNC_HOME"] = _HOME
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)

from mediasync import config  # noqa: E402  (must come after MEDIASYNC_HOME is set)

config.apply(config.Settings(shows_dir=f"{_HOME}/Shows", movies_dir=f"{_HOME}/Movies",
                             downloads_dir=f"{_HOME}/downloads", gentle=False))
