"""Optional Kodi integration: ask Kodi to rescan its video library via JSON-RPC."""
from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request

from . import config

log = logging.getLogger(__name__)


def scan_library() -> None:
    """Triggers VideoLibrary.Scan when a Kodi URL is configured; failures are only logged."""
    url = config.settings.kodi_url
    if not url:
        return
    payload = json.dumps({"jsonrpc": "2.0", "method": "VideoLibrary.Scan", "id": 1}).encode()
    request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    if config.settings.kodi_password:
        token = base64.b64encode(f"{config.settings.kodi_username}:{config.settings.kodi_password}".encode()).decode()
        request.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(request, timeout=10):
            pass
        log.info("Asked Kodi to rescan its video library")
    except (urllib.error.URLError, OSError) as e:
        log.warning("Could not reach Kodi at %s: %s", url, e)
