"""Tests never touch the real settings, cache or library: before the app is imported, everything
is pointed at a throw-away folder (individual tests then use their own temporary folders)."""
import atexit
import logging
import os
import shutil
import tempfile

_HOME = tempfile.mkdtemp(prefix="mediasync-tests-")
os.environ["MEDIASYNC_HOME"] = _HOME
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)
logging.getLogger("mediasync").addHandler(logging.NullHandler())  # no stray log lines in the test output

from mediasync import config  # noqa: E402  (must come after MEDIASYNC_HOME is set)

config.apply(config.Settings(libraries=[config.Library("Main", f"{_HOME}/Shows", f"{_HOME}/Movies")],
                             downloads_dir=f"{_HOME}/downloads", gentle=False))
