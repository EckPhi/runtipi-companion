from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path
from typing import Optional

from . import __version__

PYPI_URL = "https://pypi.org/pypi/runtipi-companion/json"
CACHE_PATH = Path.home() / ".cache" / "runtipi-companion" / "update_check.json"
CHECK_INTERVAL = 24 * 60 * 60  # once a day is plenty for a version nudge


def _parse_version(v: str) -> tuple:
    """Leading dotted-numeric prefix only (e.g. "1.2.3.dev4" -> (1, 2, 3)).
    Good enough for "is there a newer release" without a packaging dependency.
    """
    parts = []
    for token in v.split("."):
        if token.isdigit():
            parts.append(int(token))
        else:
            break
    return tuple(parts)


def _fetch_latest(timeout: float = 2.0) -> Optional[str]:
    try:
        with urllib.request.urlopen(PYPI_URL, timeout=timeout) as resp:
            data = json.loads(resp.read())
        return data["info"]["version"]
    except Exception:
        return None


def _read_cache() -> Optional[dict]:
    try:
        return json.loads(CACHE_PATH.read_text())
    except Exception:
        return None


def _write_cache(latest: Optional[str]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps({"checked_at": time.time(), "latest": latest}))
    except Exception:
        pass


def check_for_update(*, force: bool = False) -> Optional[str]:
    """Best-effort check against PyPI. Returns the latest version string if
    it's newer than what's installed, else None. Never raises -- a broken or
    offline network shouldn't break the CLI. Skipped entirely for source
    checkouts (version "0.0.0+unknown") since there's nothing to compare.
    """
    if __version__ == "0.0.0+unknown":
        return None

    cache = None if force else _read_cache()
    if cache and time.time() - cache.get("checked_at", 0) < CHECK_INTERVAL:
        latest = cache.get("latest")
    else:
        latest = _fetch_latest()
        _write_cache(latest)

    if not latest:
        return None
    if _parse_version(latest) > _parse_version(__version__):
        return latest
    return None
