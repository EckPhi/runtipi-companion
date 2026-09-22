"""Small container runtime: health endpoint plus calendar backup scheduler.

Host setup/security commands deliberately remain CLI-only. The container
does one job: run verified backups and send them through rclone's RC API.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from .backup.runner import run_backup
from .config import load_config
from .system.notify import notify

CONFIG_PATH = Path(os.environ.get("RUNTIPI_COMPANION_CONFIG", "/config/config.yaml"))
STATE_PATH = Path(os.environ.get("RUNTIPI_COMPANION_STATE", "/config/scheduler-state.json"))


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def write_managed_config() -> Path:
    """Render container configuration from Runtipi-managed environment."""
    config = {
        "version": 4,
        "runtipi": {"path": _env("RUNTIPI_PATH", "/runtipi"), "apps": []},
        "backup": {
            "local_path": _env("BACKUP_LOCAL_PATH", "/runtipi/backups"),
            "host_label": os.environ.get("BACKUP_HOST_LABEL") or None,
            "stop_apps": True,
            "sleep_duration": int(_env("BACKUP_SLEEP_DURATION", "10")),
            "schedules": {
                "daily": {"retention": int(_env("BACKUP_DAILY_RETENTION", "7"))},
                "weekly": {"retention": int(_env("BACKUP_WEEKLY_RETENTION", "4"))},
                "monthly": {"retention": int(_env("BACKUP_MONTHLY_RETENTION", "6"))},
                "yearly": {"retention": int(_env("BACKUP_YEARLY_RETENTION", "2"))},
            },
            "remotes": [
                {
                    "name": "rclone-api",
                    "rclone_remote": _env("RCLONE_REMOTE", "encrypted:runtipi-backups"),
                    "api_url": _env("RCLONE_API_URL", "http://rclone:5533"),
                    "api_username": _env("RCLONE_API_USERNAME", "rclone-admin"),
                    "api_password_env": "RCLONE_API_PASSWORD",
                    "schedules": {
                        "daily": {"retention": int(_env("REMOTE_DAILY_RETENTION", "14"))},
                        "weekly": {"retention": int(_env("REMOTE_WEEKLY_RETENTION", "8"))},
                        "monthly": {"retention": int(_env("REMOTE_MONTHLY_RETENTION", "12"))},
                        "yearly": {"retention": int(_env("REMOTE_YEARLY_RETENTION", "3"))},
                    },
                }
            ],
            # Never stop either half of the backup pipeline while it is in use.
            "app_settings": {
                "runtipi-companion": {"keep_running": True},
                "rclone": {"keep_running": True},
            },
        },
        "notify": {"urls": []},
    }
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(yaml.safe_dump(config, sort_keys=False))
    return CONFIG_PATH


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, sort_keys=True))


def _due_schedules(now: datetime, state: dict) -> list[str]:
    hour = int(_env("BACKUP_HOUR", "3"))
    if now.hour != hour:
        return []
    candidates = ["daily"]
    if now.weekday() == 6:
        candidates.append("weekly")
    if now.day == 1:
        candidates.append("monthly")
    if now.month == 1 and now.day == 1:
        candidates.append("yearly")
    today = now.date().isoformat()
    return [schedule for schedule in candidates if state.get(schedule) != today]


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path not in ("/", "/healthz"):
            self.send_error(404)
            return
        body = b"runtipi-companion scheduler is running\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        return


def run() -> None:
    config_path = write_managed_config()
    port = int(_env("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    while True:
        now = datetime.now().astimezone()
        state = _load_state()
        for schedule in _due_schedules(now, state):
            cfg = load_config(str(config_path))
            # Record the attempt before starting so a failure cannot hammer
            # the host every 30 seconds for the rest of the scheduled hour.
            state[schedule] = now.date().isoformat()
            _save_state(state)
            try:
                created = run_backup(cfg, schedule, dry_run=False)
                notify(
                    cfg.notify,
                    f"runtipi-companion: {schedule} backup completed ({len(created)} archives)",
                    success=True,
                )
            except Exception as e:
                notify(cfg.notify, f"runtipi-companion: {schedule} backup FAILED: {e}", success=False)
                print(f"{schedule} backup failed: {e}", flush=True)
        time.sleep(30)


if __name__ == "__main__":
    run()
