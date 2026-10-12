"""The `mediasync` command. Without arguments it starts the dashboard; subcommands do the same
work from a terminal (or a scheduled job) using the same engine."""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import threading
import webbrowser

from . import __version__, config, library
from .engine import Engine
from .library import Show
from .subscriptions import MODES, subscriptions
from .sync import Task, tasks_for_all
from .util import format_bytes

# how the user started us: "mediasync" (installed) or "python3 run.py" (launcher sets MEDIASYNC_PROG)
PROG = os.environ.get("MEDIASYNC_PROG", "mediasync")

EPILOG = """examples:
  {prog}                                  open the dashboard in your browser
  {prog} add https://www.youtube.com/playlist?list=...
  {prog} update                           check every subscribed show
  {prog} repair "Backyard Pottery Club"   verify and fix one item
  {prog} restore "Backyard Pottery Club"  download videos you deleted again
"""


def guess_mode(url: str) -> str:
    from .ytdl import is_channel_url
    if "list=" in url:
        return "playlist"
    return "years" if is_channel_url(url) else "movie"


def _entries_by_name(names: list[str]) -> list[dict]:
    by_name = {e["name"]: e for e in library.catalog(subscriptions.all())}
    missing = [n for n in names if n not in by_name]
    if missing:
        sys.exit(f"Not found: {', '.join(missing)}  (see `{PROG} list`)")
    return [by_name[n] for n in names]


def _tasks(kind: str, names: list[str]) -> list[Task]:
    if names:
        return [Task(kind, url=e["url"], name=e["name"], mode=e["mode"], limit=e["limit"])
                for e in _entries_by_name(names)]
    return tasks_for_all(kind)


def _ensure_folders() -> None:
    for directory in (config.STAGING_DIR, config.SHOWS_DIR, config.MOVIES_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def run_tasks(tasks: list[Task]) -> int:
    if not tasks:
        print("Nothing to do.")
        return 0
    _ensure_folders()
    engine = Engine()
    for task in tasks:
        engine.enqueue(task)
    try:
        engine.wait_until_idle()
    except KeyboardInterrupt:
        print("\nStopping after the current file... (Ctrl+C again to quit immediately)")
        engine.stop()
        engine.wait_until_idle()
    reports = list(engine.history)
    print("\nSummary:")
    for report in reversed(reports):
        print(f"  [{report.status}] {report.name}: {report.summary()}")
        for err in report.errors:
            print(f"      - {err}")
    return 1 if any(r.status == "error" for r in reports) else 0


def cmd_serve(args) -> int:
    from .web import create_app
    host = args.host or config.settings.host
    port = args.port or config.settings.port
    url = f"http://{'localhost' if host in ('0.0.0.0', '127.0.0.1', '::') else host}:{port}"
    print(f"Media Sync Archiver {__version__}")
    print(f"  dashboard:  {url}")
    print(f"  settings:   {config.config_file()}")
    print(f"  shows:      {config.SHOWS_DIR}")
    print(f"  movies:     {config.MOVIES_DIR}")
    _ensure_folders()
    if not args.no_browser:
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()
    try:
        create_app(Engine(scheduler=True)).run(host=host, port=port, debug=False, threaded=True)
    except OSError as e:
        print(f"Could not start the dashboard on port {port}: {e}\n"
              "Is it already running? Use --port to pick another port.", file=sys.stderr)
        return 1
    return 0


def cmd_add(args) -> int:
    mode = args.mode or guess_mode(args.url)
    return run_tasks([Task("update", url=args.url, name=args.name, mode=mode, limit=args.limit)])


def cmd_update(args) -> int:
    tasks = _tasks("update", args.names)
    for task in tasks:
        task.limit = args.limit or task.limit
        task.refresh_metadata = args.refresh_metadata
    return run_tasks(tasks)


def cmd_repair(args) -> int:
    return run_tasks(_tasks("repair", args.names))


def cmd_restore(args) -> int:
    return run_tasks(_tasks("restore", args.names))


def cmd_list(args) -> int:
    entries = library.catalog(subscriptions.all())
    if not entries:
        print(f"Nothing yet. Add something with: {PROG} add <YouTube URL>")
        return 0
    labels = {"playlist": "playlist", "years": "by year", "movie": "movie"}
    width = min(max(len(e["name"]) for e in entries), 60)
    print(f"{'NAME':<{width}}  {'TYPE':<8}  {'VIDEOS':>6}  {'DELETED':>7}  {'SIZE':>9}  STATUS")
    for e in entries:
        status = {"library": "", "downloads": "unfinished", "missing": "not downloaded"}[e["location"]]
        if not e["subscribed"]:
            status = (status + ", not subscribed").strip(", ")
        print(f"{e['name'][:width]:<{width}}  {labels[e['mode']]:<8}  {e['videos']:>6}  {e['deleted']:>7}  "
              f"{format_bytes(e['size']) if e['size'] else '':>9}  {status}")
    return 0


def cmd_clean(args) -> int:
    entries = _entries_by_name(args.names) if args.names else library.catalog(subscriptions.all())
    junk = []
    for entry in entries:
        show = Show(entry["name"], entry["mode"])
        junk += library.find_junk(show)
        if show.staging_dir.is_dir():
            junk.append(show.staging_dir)
    if not junk:
        print("Nothing to clean.")
        return 0
    print("These leftovers can be deleted (folders are unfinished downloads that would otherwise resume):")
    for path in junk:
        print(f"  {path}")
    try:
        answer = input("Delete them? [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    if answer not in ("y", "yes"):
        print("Nothing deleted.")
        return 0
    for path in junk:
        shutil.rmtree(path) if path.is_dir() else path.unlink(missing_ok=True)
    print(f"Deleted {len(junk)} item(s).")
    return 0


def cmd_config(args) -> int:
    print(f"settings:       {config.config_file()}")
    print(f"subscriptions:  {config.SUBSCRIPTIONS_FILE}")
    print(f"shows folder:   {config.SHOWS_DIR}")
    print(f"movies folder:  {config.MOVIES_DIR}")
    print(f"downloads:      {config.STAGING_DIR}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG, epilog=EPILOG.format(prog=PROG), formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Media Sync Archiver: keep YouTube channels, playlists and videos as an offline "
                    "Kodi/Jellyfin library.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command")

    p = sub.add_parser("serve", help="open the dashboard (the default)")
    p.add_argument("--host", help="address to listen on (default from settings)")
    p.add_argument("--port", type=int, help="port to listen on (default from settings)")
    p.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("add", help="add a channel, playlist or video and download it")
    p.add_argument("url")
    p.add_argument("--name", help="folder name (default: taken from YouTube)")
    p.add_argument("--as", dest="mode", choices=MODES,
                   help="movie, playlist (one season in playlist order) or years (one season per year)")
    p.add_argument("--limit", type=int, help="only the newest N videos")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("update", help="get new videos and fill in anything missing (default: all subscribed shows)")
    p.add_argument("names", nargs="*")
    p.add_argument("--limit", type=int, help="only look at the newest N videos")
    p.add_argument("--refresh-metadata", action="store_true",
                   help="re-download titles/descriptions of every video and rebuild the NFO files")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("repair", help="verify every file and fix everything (default: whole library)")
    p.add_argument("names", nargs="*")
    p.set_defaults(func=cmd_repair)

    p = sub.add_parser("restore", help="download videos you deleted again")
    p.add_argument("names", nargs="+")
    p.set_defaults(func=cmd_restore)

    sub.add_parser("list", help="show subscriptions and what is in the library").set_defaults(func=cmd_list)

    p = sub.add_parser("clean", help="delete leftover junk and unfinished downloads (asks first)")
    p.add_argument("names", nargs="*")
    p.set_defaults(func=cmd_clean)

    sub.add_parser("config", help="show where settings, subscriptions and videos live").set_defaults(func=cmd_config)
    return parser


COMMANDS = ("serve", "add", "update", "repair", "restore", "list", "clean", "config")


def normalize_argv(argv: list[str]) -> list[str]:
    """`mediasync` and `mediasync --port 9000` mean `mediasync serve ...`."""
    if argv and (argv[0] in COMMANDS or argv[0] in ("-h", "--help", "--version")):
        return argv
    return ["serve", *argv]


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(normalize_argv(list(sys.argv[1:] if argv is None else argv)))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    try:
        config.apply(config.load())
    except (ValueError, OSError) as e:
        sys.exit(f"Could not read {config.config_file()}: {e}")
    sys.exit(args.func(args))
