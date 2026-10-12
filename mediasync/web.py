"""Flask web dashboard and its JSON API."""
from __future__ import annotations

from dataclasses import asdict, replace

from flask import Flask, jsonify, render_template, request

from . import __version__, config, library
from .engine import Engine
from .subscriptions import normalize_limit, normalize_mode, subscriptions
from .sync import Task
from .util import sanitize_filename
from .ytdl import normalize_url

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
        return jsonify(engine.status())

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
        task = Task("update", url=url, name=name or None,
                    mode=normalize_mode(data.get("mode")), limit=normalize_limit(data.get("limit")))
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
        if kind in ("update", "repair", "restore"):
            task = Task(kind, url=entry["url"], name=entry["name"], mode=entry["mode"], limit=entry["limit"])
            if not engine.enqueue(task):
                return _error(f"{kind.capitalize()} of '{name}' is already queued.", 409)
            return jsonify(message=f"Queued {kind} of '{name}'")
        if kind == "unsubscribe":
            subscriptions.remove(name)
            return jsonify(message=f"'{name}' will no longer be synced (files were kept).")
        return _error("Unknown action.")

    @app.post("/api/sync-all")
    def sync_all():
        kind = payload().get("action", "update")
        if kind not in ("update", "repair"):
            return _error("Unknown action.")
        count = engine.enqueue_all(kind)
        return jsonify(message=f"Queued {kind} of {count} item{'s' if count != 1 else ''}.")

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

    @app.get("/api/settings")
    def get_settings():
        return jsonify(settings=asdict(config.settings), config_file=str(config.config_file()),
                       version=__version__)

    @app.post("/api/settings")
    def save_settings():
        current = asdict(config.settings)
        try:
            new = config.from_dict({**current, **payload()})
            config.save(new)
        except (ValueError, TypeError) as e:
            return _error(str(e))
        config.apply(new)
        engine.apply_settings()
        restart = [k for k in _RESTART_SETTINGS if current[k] != getattr(new, k)]
        message = "Settings saved." + (" Restart the app for the new address to take effect." if restart else "")
        return jsonify(message=message, settings=asdict(new))

    return app
