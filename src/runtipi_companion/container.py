"""Container runtime, authenticated dashboard, and backup scheduler."""

from __future__ import annotations

import hmac
import html
import json
import os
import secrets
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs

import yaml

from .backup.runner import run_backup
from .config import load_config
from .system.notify import notify

CONFIG_PATH = Path(os.environ.get("RUNTIPI_COMPANION_CONFIG", "/config/config.yaml"))
STATE_PATH = Path(os.environ.get("RUNTIPI_COMPANION_STATE", "/config/scheduler-state.json"))
STATE_LOCK = threading.Lock()


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


def _load_state_unlocked() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _load_state() -> dict:
    with STATE_LOCK:
        return _load_state_unlocked()


def _set_state_value(key: str, value: object) -> None:
    with STATE_LOCK:
        state = _load_state_unlocked()
        state[key] = value
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = STATE_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, sort_keys=True))
        temporary.replace(STATE_PATH)


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


class BackupCoordinator:
    """Serialize scheduled/manual backups and retain UI-visible outcomes."""

    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.lock = threading.Lock()
        self.active_schedule: Optional[str] = None
        self.progress_lock = threading.Lock()

    def start(self, schedule: str, *, scheduled: bool = False) -> bool:
        if schedule not in ("daily", "weekly", "monthly", "yearly"):
            raise ValueError(f"Unknown schedule: {schedule}")
        if not self.lock.acquire(blocking=False):
            return False
        self.active_schedule = schedule
        if scheduled:
            _set_state_value(schedule, datetime.now().astimezone().date().isoformat())
        threading.Thread(target=self._run, args=(schedule,), daemon=True).start()
        return True

    def _run(self, schedule: str) -> None:
        started = datetime.now().astimezone()
        outcome = {
            "schedule": schedule,
            "started_at": started.isoformat(timespec="seconds"),
            "status": "running",
        }
        self._store_outcome(outcome)
        try:
            cfg = load_config(str(self.config_path))
            created = run_backup(
                cfg, schedule, dry_run=False, progress=lambda update: self._update_progress(outcome, update)
            )
            outcome.update(status="success", archives=len(created))
            notify(
                cfg.notify,
                f"runtipi-companion: {schedule} backup completed ({len(created)} archives)",
                success=True,
            )
        except Exception as e:
            outcome.update(status="failed", error=str(e))
            if "cfg" in locals():
                notify(cfg.notify, f"runtipi-companion: {schedule} backup FAILED: {e}", success=False)
            print(f"{schedule} backup failed: {e}", flush=True)
        finally:
            outcome["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            self._store_outcome(outcome)
            self.active_schedule = None
            self.lock.release()

    @staticmethod
    def _store_outcome(outcome: dict) -> None:
        _set_state_value("last_run", outcome)

    def _update_progress(self, outcome: dict, update: dict) -> None:
        with self.progress_lock:
            event = {
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "message": str(update.get("message", update.get("stage", "Working"))),
            }
            outcome.update(update)
            outcome["events"] = [*(outcome.get("events") or []), event][-50:]
            self._store_outcome(outcome.copy())


def _latest_backups(config_path: Path, limit: int = 20) -> list[dict]:
    cfg = load_config(str(config_path))
    root = Path(cfg.backup_local_path)
    found = []
    for path in root.glob("*/*/*.tar.gz"):
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        found.append((stat.st_mtime, path, stat.st_size))
    found.sort(reverse=True, key=lambda item: item[0])
    return [
        {
            "name": path.name,
            "app": path.parent.name,
            "store": path.parent.parent.name,
            "size": size,
            "modified": datetime.fromtimestamp(modified).astimezone().isoformat(timespec="minutes"),
        }
        for modified, path, size in found[:limit]
    ]


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def _dashboard(coordinator: BackupCoordinator, csrf_token: str, message: str = "") -> bytes:
    state = _load_state()
    last = state.get("last_run") or {}
    active = coordinator.active_schedule is not None
    status = last.get("status", "No backups recorded")
    cfg = load_config(str(coordinator.config_path))
    remote = cfg.backup.remotes[0]
    rows = (
        "".join(
            "<tr>"
            f"<td>{html.escape(item['app'])}</td><td>{html.escape(item['store'])}</td>"
            f"<td>{html.escape(item['name'])}</td><td>{_human_size(item['size'])}</td>"
            f"<td>{html.escape(item['modified'])}</td></tr>"
            for item in _latest_backups(coordinator.config_path)
        )
        or '<tr><td colspan="5" class="muted">No local backups yet</td></tr>'
    )
    buttons = "".join(
        f'<button name="schedule" value="{schedule}" type="submit">Run {schedule}</button>'
        for schedule in ("daily", "weekly", "monthly", "yearly")
    )
    notice = f'<p class="notice">{html.escape(message)}</p>' if message else ""
    total_apps = int(last.get("total_apps") or 0)
    completed_apps = int(last.get("completed_apps") or 0)
    percent = min(100, round(completed_apps * 100 / total_apps)) if total_apps else 0
    progress_detail = html.escape(str(last.get("message") or last.get("stage") or "Waiting for progress"))
    current_app = html.escape(str(last.get("app") or "—"))
    error = (
        f'<div class="error"><strong>Last error</strong><br>{html.escape(str(last["error"]))}</div>'
        if last.get("error")
        else ""
    )
    events = last.get("events") or []
    event_log = "\n".join(f"{event.get('at', '')}  {event.get('message', '')}" for event in events)
    debug = html.escape(event_log or "No diagnostic events recorded yet")
    refresh = '<meta http-equiv="refresh" content="3">' if active else ""
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">{refresh}
<title>Runtipi Companion</title><style>
:root{{--bg:#0b1220;--panel:#131d30;--line:#26344d;--text:#eef4ff;--muted:#9fb0c9;--accent:#2dd4bf;--amber:#fbbf24}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:15px system-ui,sans-serif}}
main{{max-width:1100px;margin:auto;padding:32px 20px}}h1{{margin:0 0 6px;font-size:30px}}h2{{font-size:18px}}
.muted{{color:var(--muted)}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin:24px 0}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px}}.value{{font-size:18px;font-weight:650}}
button{{background:var(--accent);color:#06241f;border:0;border-radius:8px;padding:10px 14px;font-weight:700;cursor:pointer;margin:4px}}
button:hover{{filter:brightness(1.08)}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;padding:10px;border-bottom:1px solid var(--line)}}
.notice,.error{{border-left:3px solid var(--amber);padding:10px 14px;background:var(--panel)}}code{{color:var(--accent)}}
.progress{{height:12px;background:var(--line);border-radius:999px;overflow:hidden;margin:12px 0}}.progress span{{display:block;height:100%;background:var(--accent)}}
pre{{background:#09101c;border:1px solid var(--line);border-radius:8px;padding:14px;overflow:auto;white-space:pre-wrap;max-height:320px}}
</style></head><body><main><h1>Runtipi Companion</h1><p class="muted">Verified backups through rclone</p>{notice}
<section class="grid"><div class="card"><div class="muted">Status</div><div class="value">{html.escape(str(status))}</div></div>
<div class="card"><div class="muted">Scheduled hour</div><div class="value">{html.escape(_env('BACKUP_HOUR', '3'))}:00</div></div>
<div class="card"><div class="muted">Remote</div><div class="value"><code>{html.escape(remote.rclone_remote)}</code></div></div>
<div class="card"><div class="muted">Last finished</div><div class="value">{html.escape(last.get('finished_at', 'Never'))}</div></div></section>
<section class="card"><h2>Backup progress</h2><div class="value">{progress_detail}</div>
<div class="progress" role="progressbar" aria-valuenow="{percent}" aria-valuemin="0" aria-valuemax="100"><span style="width:{percent}%"></span></div>
<div class="muted">{completed_apps} of {total_apps} apps complete · Current app: {current_app}</div>{error}</section>
<section class="card"><h2>Run a backup</h2><form method="post" action="/backup"><input type="hidden" name="csrf" value="{csrf_token}">{buttons}</form></section>
<section class="card" style="margin-top:14px"><h2>Recent local backups</h2><div style="overflow:auto"><table><thead><tr><th>App</th><th>Store</th><th>Archive</th><th>Size</th><th>Created</th></tr></thead><tbody>{rows}</tbody></table></div></section>
<details class="card" style="margin-top:14px"><summary><strong>Diagnostics</strong></summary><pre>{debug}</pre></details>
</main></body></html>"""
    return page.encode()


def build_handler(coordinator: BackupCoordinator, csrf_token: str):
    class WebHandler(BaseHTTPRequestHandler):
        def _send_dashboard(self, message: str = "") -> None:
            body = _dashboard(coordinator, csrf_token, message)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path == "/healthz":
                body = b"ok\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path != "/":
                self.send_error(404)
                return
            self._send_dashboard()

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path != "/backup":
                self.send_error(404)
                return
            length = min(int(self.headers.get("Content-Length", "0")), 4096)
            form = parse_qs(self.rfile.read(length).decode())
            if not hmac.compare_digest(form.get("csrf", [""])[0], csrf_token):
                self.send_error(403, "Invalid CSRF token")
                return
            schedule = form.get("schedule", [""])[0]
            try:
                started = coordinator.start(schedule)
            except ValueError:
                self.send_error(400, "Invalid schedule")
                return
            self._send_dashboard(f"{schedule.title()} backup started" if started else "A backup is already running")

        def log_message(self, format: str, *args) -> None:
            return

    return WebHandler


def run() -> None:
    config_path = write_managed_config()
    coordinator = BackupCoordinator(config_path)
    port = int(_env("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), build_handler(coordinator, secrets.token_urlsafe(32)))
    threading.Thread(target=server.serve_forever, daemon=True).start()

    while True:
        now = datetime.now().astimezone()
        state = _load_state()
        for schedule in _due_schedules(now, state):
            coordinator.start(schedule, scheduled=True)
        time.sleep(30)


if __name__ == "__main__":
    run()
