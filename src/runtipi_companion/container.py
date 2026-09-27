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

from .backup.rclone import probe_remote_upload
from .backup.runner import discover_apps, run_backup
from .config import load_config
from .system.notify import notify

CONFIG_PATH = Path(os.environ.get("RUNTIPI_COMPANION_CONFIG", "/config/config.yaml"))
STATE_PATH = Path(os.environ.get("RUNTIPI_COMPANION_STATE", "/config/scheduler-state.json"))
SETTINGS_PATH = Path(os.environ.get("RUNTIPI_COMPANION_SETTINGS", "/config/dashboard-settings.json"))
STATE_LOCK = threading.Lock()
SETTINGS_LOCK = threading.Lock()


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def _default_settings() -> dict:
    return {
        "backup_hour": int(_env("BACKUP_HOUR", "3")),
        "enabled_schedules": ["daily", "weekly", "monthly", "yearly"],
        "rclone_remote": _env("RCLONE_REMOTE", "encrypted:runtipi-backups"),
        "local_retention": {
            "daily": int(_env("BACKUP_DAILY_RETENTION", "7")),
            "weekly": int(_env("BACKUP_WEEKLY_RETENTION", "4")),
            "monthly": int(_env("BACKUP_MONTHLY_RETENTION", "6")),
            "yearly": int(_env("BACKUP_YEARLY_RETENTION", "2")),
        },
        "remote_retention": {
            "daily": int(_env("REMOTE_DAILY_RETENTION", "14")),
            "weekly": int(_env("REMOTE_WEEKLY_RETENTION", "8")),
            "monthly": int(_env("REMOTE_MONTHLY_RETENTION", "12")),
            "yearly": int(_env("REMOTE_YEARLY_RETENTION", "3")),
        },
        "excluded_apps": [],
    }


def _load_settings() -> dict:
    defaults = _default_settings()
    with SETTINGS_LOCK:
        try:
            saved = json.loads(SETTINGS_PATH.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return defaults
    for key in ("backup_hour", "enabled_schedules", "rclone_remote", "excluded_apps"):
        if key in saved:
            defaults[key] = saved[key]
    for key in ("local_retention", "remote_retention"):
        defaults[key].update(saved.get(key) or {})
    return defaults


def _save_settings(settings: dict) -> None:
    with SETTINGS_LOCK:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = SETTINGS_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(settings, indent=2, sort_keys=True))
        temporary.replace(SETTINGS_PATH)


def write_managed_config() -> Path:
    """Render container configuration from Runtipi-managed environment."""
    settings = _load_settings()
    config = {
        "version": 4,
        "runtipi": {"path": _env("RUNTIPI_PATH", "/runtipi"), "apps": []},
        "backup": {
            "local_path": _env("BACKUP_LOCAL_PATH", "/runtipi/backups"),
            "host_label": os.environ.get("BACKUP_HOST_LABEL") or None,
            "stop_apps": True,
            "sleep_duration": int(_env("BACKUP_SLEEP_DURATION", "10")),
            "schedules": {
                schedule: {"retention": settings["local_retention"][schedule]}
                for schedule in ("daily", "weekly", "monthly", "yearly")
            },
            "remotes": [
                {
                    "name": "rclone-api",
                    "rclone_remote": settings["rclone_remote"],
                    "api_url": _env("RCLONE_API_URL", "http://rclone:5533"),
                    "api_username": _env("RCLONE_API_USERNAME", "rclone-admin"),
                    "api_password_env": "RCLONE_API_PASSWORD",
                    "schedules": {
                        schedule: {"retention": settings["remote_retention"][schedule]}
                        for schedule in ("daily", "weekly", "monthly", "yearly")
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
    settings = _load_settings()
    hour = int(settings["backup_hour"])
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
    enabled = set(settings["enabled_schedules"])
    return [schedule for schedule in candidates if schedule in enabled and state.get(schedule) != today]


class BackupCoordinator:
    """Serialize scheduled/manual backups and retain UI-visible outcomes."""

    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.lock = threading.Lock()
        self.active_schedule: Optional[str] = None
        self.progress_lock = threading.Lock()

    def start(self, schedule: str, *, scheduled: bool = False, app_ref: Optional[str] = None) -> bool:
        if schedule not in ("daily", "weekly", "monthly", "yearly"):
            raise ValueError(f"Unknown schedule: {schedule}")
        if app_ref:
            cfg = load_config(str(self.config_path))
            installed = {ref.ref for ref in discover_apps(cfg.runtipi.path)}
            if app_ref not in installed:
                raise ValueError(f"Unknown app: {app_ref}")
        if not self.lock.acquire(blocking=False):
            return False
        self.active_schedule = schedule
        if scheduled:
            _set_state_value(schedule, datetime.now().astimezone().date().isoformat())
        threading.Thread(target=self._run, args=(schedule, app_ref), daemon=True).start()
        return True

    def _run(self, schedule: str, app_ref: Optional[str] = None) -> None:
        started = datetime.now().astimezone()
        outcome = {
            "schedule": schedule,
            "started_at": started.isoformat(timespec="seconds"),
            "status": "running",
            "target": app_ref or "all apps",
        }
        self._store_outcome(outcome)
        try:
            cfg = load_config(str(self.config_path))
            apps = [app_ref] if app_ref else None
            if app_ref is None:
                excluded = set(_load_settings()["excluded_apps"])
                if excluded:
                    apps = [ref.ref for ref in discover_apps(cfg.runtipi.path) if ref.ref not in excluded]
            created = run_backup(
                cfg,
                schedule,
                apps=apps,
                dry_run=False,
                progress=lambda update: self._update_progress(outcome, update),
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


def _latest_backups(config_path: Path, limit: Optional[int] = 20) -> list[dict]:
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
        for modified, path, size in (found[:limit] if limit is not None else found)
    ]


def _installed_apps(config_path: Path) -> list:
    cfg = load_config(str(config_path))
    try:
        return discover_apps(cfg.runtipi.path)
    except RuntimeError:
        return []


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def _backup_rows(backups: list[dict]) -> str:
    return (
        "".join(
            "<tr>"
            f"<td>{html.escape(item['app'])}</td><td>{html.escape(item['store'])}</td>"
            f"<td>{html.escape(item['name'])}</td><td>{_human_size(item['size'])}</td>"
            f"<td>{html.escape(item['modified'])}</td></tr>"
            for item in backups
        )
        or '<tr><td colspan="5" class="muted">No local backups yet</td></tr>'
    )


def _dashboard(coordinator: BackupCoordinator, csrf_token: str, message: str = "") -> bytes:
    state = _load_state()
    last = state.get("last_run") or {}
    active = coordinator.active_schedule is not None
    disabled = " disabled" if active else ""
    status = last.get("status", "No backups recorded")
    cfg = load_config(str(coordinator.config_path))
    remote = cfg.backup.remotes[0]
    rows = _backup_rows(_latest_backups(coordinator.config_path))
    app_rows = (
        "".join(
            "<tr>"
            f"<td>{html.escape(ref.app_id)}</td><td>{html.escape(ref.store)}</td>"
            '<td><form method="post" action="/backup">'
            f'<input type="hidden" name="csrf" value="{csrf_token}">'
            '<input type="hidden" name="schedule" value="daily">'
            f'<input type="hidden" name="app" value="{html.escape(ref.ref, quote=True)}">'
            f'<button type="submit"{disabled}>Back up now</button></form></td></tr>'
            for ref in _installed_apps(coordinator.config_path)
        )
        or '<tr><td colspan="3" class="muted">No installed apps discovered</td></tr>'
    )
    buttons = "".join(
        f'<button name="schedule" value="{schedule}" type="submit"{disabled}>Run {schedule}</button>'
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
button:hover{{filter:brightness(1.08)}}button:disabled{{cursor:not-allowed;opacity:.5}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;padding:10px;border-bottom:1px solid var(--line)}}
.notice,.error{{border-left:3px solid var(--amber);padding:10px 14px;background:var(--panel)}}code{{color:var(--accent)}}
a{{color:var(--accent)}}form{{margin:0}}
.progress{{height:12px;background:var(--line);border-radius:999px;overflow:hidden;margin:12px 0}}.progress span{{display:block;height:100%;background:var(--accent)}}
pre{{background:#09101c;border:1px solid var(--line);border-radius:8px;padding:14px;overflow:auto;white-space:pre-wrap;max-height:320px}}
</style></head><body><main><h1>Runtipi Companion</h1><p class="muted">Verified backups through rclone · <a href="/config">Configuration</a></p>{notice}
<section class="grid"><div class="card"><div class="muted">Status</div><div class="value">{html.escape(str(status))}</div></div>
<div class="card"><div class="muted">Scheduled hour</div><div class="value">{_load_settings()['backup_hour']}:00</div></div>
<div class="card"><div class="muted">Remote</div><div class="value"><code>{html.escape(remote.rclone_remote)}</code></div></div>
<div class="card"><div class="muted">Last finished</div><div class="value">{html.escape(last.get('finished_at', 'Never'))}</div></div></section>
<section class="card"><h2>Backup progress</h2><div class="value">{progress_detail}</div>
<div class="progress" role="progressbar" aria-valuenow="{percent}" aria-valuemin="0" aria-valuemax="100"><span style="width:{percent}%"></span></div>
<div class="muted">{completed_apps} of {total_apps} apps complete · Current app: {current_app}</div>{error}</section>
<section class="card"><h2>Run a backup</h2><form method="post" action="/backup"><input type="hidden" name="csrf" value="{csrf_token}">{buttons}</form></section>
<section class="card" style="margin-top:14px"><h2>Installed apps</h2><div style="overflow:auto"><table><thead><tr><th>App</th><th>Store</th><th>Action</th></tr></thead><tbody>{app_rows}</tbody></table></div></section>
<section class="card" style="margin-top:14px"><h2>Recent local backups</h2><p><a href="/backups">Open backup explorer</a></p><div style="overflow:auto"><table><thead><tr><th>App</th><th>Store</th><th>Archive</th><th>Size</th><th>Created</th></tr></thead><tbody>{rows}</tbody></table></div></section>
<details class="card" style="margin-top:14px"><summary><strong>Diagnostics</strong></summary><pre>{debug}</pre></details>
</main></body></html>"""
    return page.encode()


def _backup_explorer(coordinator: BackupCoordinator) -> bytes:
    backups = _latest_backups(coordinator.config_path, limit=None)
    total_size = sum(item["size"] for item in backups)
    rows = _backup_rows(backups)
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Backup Explorer · Runtipi Companion</title><style>
:root{{--bg:#0b1220;--panel:#131d30;--line:#26344d;--text:#eef4ff;--muted:#9fb0c9;--accent:#2dd4bf}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:15px system-ui,sans-serif}}
main{{max-width:1100px;margin:auto;padding:32px 20px}}a{{color:var(--accent)}}.muted{{color:var(--muted)}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px;margin-top:20px}}
table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;padding:10px;border-bottom:1px solid var(--line)}}
</style></head><body><main><p><a href="/">← Dashboard</a></p><h1>Backup Explorer</h1>
<p class="muted">{len(backups)} local archives · {_human_size(total_size)} total · read-only</p>
<section class="card"><div style="overflow:auto"><table><thead><tr><th>App</th><th>Store</th><th>Archive</th><th>Size</th><th>Created</th></tr></thead><tbody>{rows}</tbody></table></div></section>
</main></body></html>"""
    return page.encode()


def _settings_from_form(form: dict, installed_refs: set[str]) -> dict:
    try:
        backup_hour = int(form.get("backup_hour", [""])[0])
        if not 0 <= backup_hour <= 23:
            raise ValueError
        local_retention = {
            schedule: int(form.get(f"local_{schedule}", [""])[0])
            for schedule in ("daily", "weekly", "monthly", "yearly")
        }
        remote_retention = {
            schedule: int(form.get(f"remote_{schedule}", [""])[0])
            for schedule in ("daily", "weekly", "monthly", "yearly")
        }
    except (TypeError, ValueError) as e:
        raise ValueError("Hours and retained copies must be valid numbers") from e
    if any(not 1 <= value <= 1000 for value in (*local_retention.values(), *remote_retention.values())):
        raise ValueError("Retained copies must be between 1 and 1000")
    remote = form.get("rclone_remote", [""])[0].strip().rstrip("/")
    if ":" not in remote or not remote.split(":", 1)[0]:
        raise ValueError("Backup target must use the remote:path format")
    enabled = [
        schedule
        for schedule in ("daily", "weekly", "monthly", "yearly")
        if schedule in form.get("enabled_schedule", [])
    ]
    excluded = sorted(set(form.get("excluded_app", [])) & installed_refs)
    return {
        "backup_hour": backup_hour,
        "enabled_schedules": enabled,
        "rclone_remote": remote,
        "local_retention": local_retention,
        "remote_retention": remote_retention,
        "excluded_apps": excluded,
    }


def _config_page(coordinator: BackupCoordinator, csrf_token: str) -> bytes:
    settings = _load_settings()
    installed = _installed_apps(coordinator.config_path)
    enabled = set(settings["enabled_schedules"])
    excluded = set(settings["excluded_apps"])
    rclone_test = _load_state().get("rclone_test") or {}
    test_result = ""
    if rclone_test:
        test_class = "success" if rclone_test.get("status") == "success" else "error"
        test_result = (
            f'<p class="{test_class}"><strong>Last rclone test: '
            f'{html.escape(str(rclone_test.get("status", "unknown")))}</strong><br>'
            f'{html.escape(str(rclone_test.get("message", "")))}<br>'
            f'<span class="muted">{html.escape(str(rclone_test.get("at", "")))}</span></p>'
        )
    schedule_rows = "".join(
        "<tr>"
        f'<td><label><input type="checkbox" name="enabled_schedule" value="{schedule}"'
        f'{" checked" if schedule in enabled else ""}> {schedule.title()}</label></td>'
        f'<td><input type="number" name="local_{schedule}" min="1" max="1000" required '
        f'value="{settings["local_retention"][schedule]}"></td>'
        f'<td><input type="number" name="remote_{schedule}" min="1" max="1000" required '
        f'value="{settings["remote_retention"][schedule]}"></td></tr>'
        for schedule in ("daily", "weekly", "monthly", "yearly")
    )
    app_rows = (
        "".join(
            "<tr>"
            f"<td>{html.escape(ref.app_id)}</td><td>{html.escape(ref.store)}</td>"
            f'<td><input type="checkbox" name="excluded_app" value="{html.escape(ref.ref, quote=True)}"'
            f'{" checked" if ref.ref in excluded else ""} aria-label="Exclude {html.escape(ref.ref, quote=True)}"></td></tr>'
            for ref in installed
        )
        or '<tr><td colspan="3" class="muted">No installed apps discovered</td></tr>'
    )
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Configuration · Runtipi Companion</title><style>
:root{{--bg:#0b1220;--panel:#131d30;--line:#26344d;--text:#eef4ff;--muted:#9fb0c9;--accent:#2dd4bf;--red:#fb7185}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:15px system-ui,sans-serif}}
main{{max-width:900px;margin:auto;padding:32px 20px}}a{{color:var(--accent)}}.muted{{color:var(--muted)}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px;margin-top:20px}}
table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;padding:10px;border-bottom:1px solid var(--line)}}
input[type=text],input[type=number]{{width:100%;padding:9px;background:#09101c;color:var(--text);border:1px solid var(--line);border-radius:6px}}
button{{background:var(--accent);color:#06241f;border:0;border-radius:8px;padding:10px 16px;font-weight:700;cursor:pointer}}
.success,.error{{border-left:3px solid var(--accent);padding:10px 14px;background:#09101c}}.error{{border-color:var(--red)}}
</style></head><body><main><p><a href="/">← Dashboard</a></p><h1>Configuration</h1>
<form method="post" action="/config"><input type="hidden" name="csrf" value="{csrf_token}">
<section class="card"><h2>Schedule and target</h2>
<p><label>Backup hour (0–23)<input type="number" name="backup_hour" min="0" max="23" required value="{settings['backup_hour']}"></label></p>
<p><label>Rclone backup target<input type="text" name="rclone_remote" required value="{html.escape(settings['rclone_remote'], quote=True)}" placeholder="encrypted:runtipi-backups"></label></p>
{test_result}
<p class="muted">Enabled schedules control automatic runs. Retained copies apply to automatic and manual backups.</p>
<div style="overflow:auto"><table><thead><tr><th>Automatic schedule</th><th>Local copies</th><th>Remote copies</th></tr></thead><tbody>{schedule_rows}</tbody></table></div></section>
<section class="card"><h2>Excluded apps</h2><p class="muted">Excluded apps are skipped by all-app runs but remain available for individual backups.</p>
<div style="overflow:auto"><table><thead><tr><th>App</th><th>Store</th><th>Exclude</th></tr></thead><tbody>{app_rows}</tbody></table></div></section>
<p><button type="submit">Save configuration</button></p></form>
<form method="post" action="/rclone-test"><input type="hidden" name="csrf" value="{csrf_token}">
<section class="card"><h2>Test rclone upload</h2><p class="muted">Uploads and verifies a small temporary file using the saved target, then deletes it.</p>
<button type="submit">Run upload test</button></section></form></main></body></html>"""
    return page.encode()


def build_handler(coordinator: BackupCoordinator, csrf_token: str):
    class WebHandler(BaseHTTPRequestHandler):
        def _send_dashboard(self, message: str = "") -> None:
            body = _dashboard(coordinator, csrf_token, message)
            self._send_html(body)

        def _send_html(self, body: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, location: str = "/") -> None:
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

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
                if self.path == "/backups":
                    self._send_html(_backup_explorer(coordinator))
                    return
                if self.path == "/config":
                    self._send_html(_config_page(coordinator, csrf_token))
                    return
                self.send_error(404)
                return
            self._send_dashboard()

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path not in ("/backup", "/config", "/rclone-test"):
                self.send_error(404)
                return
            length = min(int(self.headers.get("Content-Length", "0")), 4096)
            form = parse_qs(self.rfile.read(length).decode())
            if not hmac.compare_digest(form.get("csrf", [""])[0], csrf_token):
                self.send_error(403, "Invalid CSRF token")
                return
            if self.path == "/rclone-test":
                if not coordinator.lock.acquire(blocking=False):
                    self.send_error(409, "A backup or rclone test is already running")
                    return
                try:
                    cfg = load_config(str(coordinator.config_path))
                    target = probe_remote_upload(cfg.backup.remotes[0])
                    result = {"status": "success", "message": f"Upload verified and test file removed: {target}"}
                except Exception as e:
                    result = {"status": "failed", "message": str(e)}
                finally:
                    coordinator.lock.release()
                result["at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                _set_state_value("rclone_test", result)
                self._redirect("/config")
                return
            if self.path == "/config":
                installed_refs = {ref.ref for ref in _installed_apps(coordinator.config_path)}
                try:
                    settings = _settings_from_form(form, installed_refs)
                except ValueError as e:
                    self.send_error(400, str(e))
                    return
                _save_settings(settings)
                write_managed_config()
                self._redirect("/config")
                return
            schedule = form.get("schedule", [""])[0]
            app_ref = form.get("app", [""])[0] or None
            try:
                coordinator.start(schedule, app_ref=app_ref)
            except ValueError:
                self.send_error(400, "Invalid backup request")
                return
            self._redirect()

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
