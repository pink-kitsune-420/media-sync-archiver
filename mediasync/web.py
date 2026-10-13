"""Flask web dashboard and its JSON API."""
from __future__ import annotations

import logging
import shutil
from dataclasses import asdict, replace

from flask import Flask, jsonify, render_template, request

from . import __version__, config, library, logs
from .engine import Engine
from .folders import clean_name, folders
from .library import Show
from .subscriptions import Subscription, normalize_limit, normalize_mode, subscriptions
from .sync import Task, task_for
from .util import sanitize_filename
from .ytdl import normalize_url

log = logging.getLogger(__name__)

# settings that only take effect after the app is restarted
_RESTART_SETTINGS = ("host", "port")


def _error(message: str, status: int = 400):
    return jsonify(error=message), status


def create_app(engine: Engine) -> Flask:
    app = Flask(__name__)

    def payload() -> dict:
        return request.get_json(silent=True) or {}

    @app.get("/")
    def index():
        return render_template("index.html", version=__version__)

    @app.get("/api/status")
    def status():
        libraries = [{"name": lib.name, "online": lib.online, "state": lib.state} for lib in config.libraries()]
        return jsonify({**engine.status(), "libraries": libraries})

    @app.get("/api/library")
    def library_list():
        return jsonify(library.catalog(subscriptions.all()))

    @app.get("/api/subscriptions")
    def subscription_list():
        return jsonify([asdict(s) for s in subscriptions.all()])

    @app.post("/api/queue")
    def queue_url():
        data = payload()
        url = (data.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return _error("Please enter a full YouTube URL (starting with https://).")
        name = sanitize_filename(data.get("name") or "")
        if not name:  # re-adding a subscribed URL reuses its folder
            existing = subscriptions.find_by_url(normalize_url(url)) or subscriptions.find_by_url(url)
            name = existing.name if existing else ""
        library_name = data.get("library") or ""
        if library_name and not config.get_library(library_name):
            return _error(f"There is no library called '{library_name}'.")
        task = Task("update", url=url, name=name or None, mode=normalize_mode(data.get("mode")),
                    limit=normalize_limit(data.get("limit")), library=library_name)
        if not engine.enqueue(task):
            return _error("That is already in the queue.", 409)
        return jsonify(message=f"Queued {task.label}")

    @app.post("/api/action")
    def action():
        data = payload()
        name, kind = data.get("name"), data.get("action")
        entry = next((e for e in library.catalog(subscriptions.all()) if e["name"] == name), None)
        if not entry:
            return _error(f"'{name}' was not found.", 404)
        if kind in ("update", "repair", "restore", "download_again"):
            if entry["location"] == "offline":
                return _error(f"'{name}' is offline: {entry['note']}")
            task = (task_for("update", entry, download_again=True) if kind == "download_again"
                    else task_for(kind, entry))
            if not engine.enqueue(task):
                return _error(f"{task.kind.capitalize()} of '{name}' is already queued.", 409)
            return jsonify(message=f"Queued {kind.replace('_', ' ')} of '{name}'")
        if kind == "unsubscribe":
            subscriptions.remove(name)
            log.info("Unsubscribed %s (its files were kept)", name)
            return jsonify(message=f"'{name}' will no longer be synced (files were kept).")
        if kind == "subscribe":
            url = entry["url"] or (data.get("url") or "").strip()  # entered by hand when it isn't known
            if entry["subscribed"]:
                return _error(f"'{name}' is already subscribed.")
            if not url.startswith(("http://", "https://")):
                return _error("Please enter its full YouTube address (starting with https://).")
            subscriptions.upsert(Subscription(name, url, entry["mode"], entry["limit"],
                                              library=entry["library"], archived=entry["location"] == "library"))
            log.info("Subscribed to %s", name)
            return jsonify(message=f"Subscribed: Update all now includes '{name}'.")
        if kind == "set_folder":
            folder = clean_name(data.get("folder"))
            folders.put(name, folder)
            return jsonify(message=f"Put '{name}' in {folder}." if folder else f"Took '{name}' out of its folder.")
        if kind == "move":
            target = config.get_library(data.get("library"))
            if entry["location"] != "library":
                return _error(f"'{name}' can only be moved while it is in a connected library.")
            if target is None or target.name == entry["library"]:
                return _error("Choose another library to move it to.")
            if not target.online:
                return _error(f"{target.name} is not connected.")
            if not engine.enqueue(task_for("move", entry, library=target.name)):
                return _error(f"'{name}' is already being moved.", 409)
            return jsonify(message=f"Queued: moving '{name}' to {target.name}")
        if kind == "delete_leftovers":
            if entry["location"] != "leftovers":
                return _error(f"'{name}' has no leftover download files.")
            if engine.busy(name):
                return _error(f"'{name}' is in the queue; try again when it's done.", 409)
            shutil.rmtree(Show(name, entry["mode"]).staging_dir, ignore_errors=True)
            log.info("Deleted the leftover download files of %s", name)
            return jsonify(message=f"Deleted the leftover download files of '{name}'.")
        return _error("Unknown action.")

    @app.get("/api/folders")
    def folder_list():
        return jsonify(folders=folders.names())

    @app.post("/api/folders/create")
    def create_folder():
        name = clean_name(payload().get("name"))
        if not name:
            return _error("Please give the folder a name.")
        if name in folders.names():
            return _error(f"There already is a folder called {name}.", 409)
        folders.create(name)
        return jsonify(message=f"Made the folder {name}: drag shows onto it, or use a show's … menu.")

    @app.post("/api/folders/rename")
    def rename_folder():
        data = payload()
        old, new = clean_name(data.get("old")), clean_name(data.get("new"))
        if not old:
            return _error("Which folder?")
        count = folders.rename(old, new)
        return jsonify(message=f"Renamed {old} to {new}." if new else
                       f"Removed the folder {old}; its {count} item{'s' if count != 1 else ''} stay in the library.")

    @app.post("/api/sync-all")
    def sync_all():
        kind = payload().get("action", "update")
        if kind not in ("update", "repair"):
            return _error("Unknown action.")
        count = engine.enqueue_all(kind)
        message = f"Queued {kind} of {count} item{'s' if count != 1 else ''}."
        offline = [lib.name for lib in config.libraries() if not lib.online]
        if offline:
            message += f" Skipped what is on {', '.join(offline)} (not connected)."
        return jsonify(message=message)

    @app.post("/api/queue/remove")
    def queue_remove():
        if not engine.remove(int(payload().get("id") or 0)):
            return _error("That task is no longer in the queue.", 404)
        return jsonify(message="Removed from the queue.")

    @app.post("/api/control")
    def control():
        handlers = {"pause": engine.pause, "resume": engine.resume, "stop": engine.stop}
        kind = payload().get("action")
        if kind not in handlers:
            return _error("Unknown control action.")
        handlers[kind]()
        return jsonify(message=kind)

    @app.post("/api/speed")
    def speed():
        limit = payload().get("limit") or ""
        try:
            engine.set_speed_limit(limit or None)
            config.save(replace(config.settings, speed_limit=limit))
        except ValueError as e:
            return _error(str(e))
        config.apply(replace(config.settings, speed_limit=limit))
        return jsonify(speed_limit=engine.speed_limit)

    def settings_payload(settings: config.Settings) -> dict:
        return dict(settings=asdict(settings), online={lib.name: lib.online for lib in settings.libraries},
                    states={lib.name: lib.state for lib in settings.libraries},
                    config_file=str(config.config_file()), log_dir=str(config.LOG_DIR), version=__version__)

    @app.get("/api/settings")
    def get_settings():
        return jsonify(settings_payload(config.settings))

    @app.post("/api/settings")
    def save_settings():
        old = config.settings
        current = asdict(old)
        try:
            new = config.from_dict({**current, **payload()})
            config.save(new)
        except (ValueError, TypeError) as e:
            return _error(str(e))
        config.apply(new)
        engine.apply_settings()
        logs.set_detailed(new.detailed_log)
        not_created = library.prepare_libraries(old.libraries, new.libraries)
        message = "Settings saved."
        if [k for k in _RESTART_SETTINGS if current[k] != getattr(new, k)]:
            message += " Restart the app for the new address to take effect."
        if not_created:
            message += (f" The folders of {', '.join(not_created)} could not be created (is the drive connected?). "
                        "Once it is, press “Create folders”.")
        return jsonify(message=message, **settings_payload(new))

    @app.post("/api/libraries/create")
    def create_library_folders():
        lib = config.get_library(payload().get("name"))
        if not lib:
            return _error("There is no such library.", 404)
        if lib.state == "not connected":
            return _error(f"{lib.name} is not connected, so its folders can't be created. Plug the drive in first.")
        try:
            for folder in (lib.shows_path, lib.movies_path):
                folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return _error(f"Could not create the folders of {lib.name}: {e}")
        return jsonify(message=f"Created the folders of {lib.name}.", **settings_payload(config.settings))

    return app
