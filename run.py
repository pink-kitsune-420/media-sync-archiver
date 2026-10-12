#!/usr/bin/env python3
"""Start Media Sync Archiver:  python3 run.py   (Windows: py run.py)

The first run creates a private Python environment in the ".venv" folder next to this file and
installs what the app needs into it (this needs internet once). Nothing outside this folder is
changed: no system packages, no PATH changes. Delete the folder and it is all gone.

    python3 run.py              open the dashboard
    python3 run.py update       any other command is passed on (see: python3 run.py --help)
    python3 run.py --upgrade    update yt-dlp and the other dependencies, then start
"""
import os
import subprocess
import sys
import venv
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV = HERE / ".venv"
PYTHON = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
STAMP = VENV / ".mediasync-installed"


def setup(upgrade: bool) -> None:
    if not PYTHON.exists():
        print("First start: setting up a private Python environment in .venv (one time, needs internet)...", flush=True)
        try:
            venv.create(VENV, with_pip=True)
        except (subprocess.CalledProcessError, OSError) as e:
            sys.exit(f"Could not create the environment ({e}).\n"
                     "On Debian/Ubuntu/Mint install the venv module first:  sudo apt install python3-venv\n"
                     "then delete the .venv folder and run this again.")
    wanted = (HERE / "pyproject.toml").read_text(encoding="utf-8")
    if upgrade or not STAMP.exists() or STAMP.read_text(encoding="utf-8") != wanted:
        print("Installing what Media Sync Archiver needs (Flask, yt-dlp, Pillow)...", flush=True)
        cmd = [str(PYTHON), "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--editable", str(HERE)]
        if upgrade:
            cmd.insert(4, "--upgrade")
        if subprocess.call(cmd) != 0:
            sys.exit("Installing failed; see the messages above.")
        STAMP.write_text(wanted, encoding="utf-8")


def main() -> None:
    if sys.version_info < (3, 10):
        sys.exit("Media Sync Archiver needs Python 3.10 or newer.")
    args = sys.argv[1:]
    upgrade = "--upgrade" in args
    args = [a for a in args if a != "--upgrade"]
    setup(upgrade)
    env = {**os.environ, "MEDIASYNC_PROG": "python3 run.py" if os.name != "nt" else "py run.py"}
    sys.exit(subprocess.call([str(PYTHON), "-m", "mediasync", *args], env=env))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
