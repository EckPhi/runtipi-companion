"""Form-based config wizard (textual TUI): text fields, checkboxes, and a
dropdown, with realtime per-field validation. The prompt-based flow in
config_wizard.py remains as the --classic fallback and for non-tty runs.

The form only *collects* answers; writing goes through
config_wizard.write_config so the same round-trip validation guards both
wizards.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from rich.console import Console
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.validation import ValidationResult, Validator
from textual.widgets import Button, Checkbox, Footer, Input, Select, Static

from ..config import CONFIG_VERSION, VALID_SCHEDULES
from . import config_wizard as cw
from . import validators as v

console = Console()

LOCAL_RETENTION_DEFAULTS = cw.LOCAL_RETENTION_DEFAULTS
REMOTE_RETENTION_DEFAULTS = cw.REMOTE_RETENTION_DEFAULTS


class FnValidator(Validator):
    """Adapt a pure `validators.py` function into a textual Validator."""

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def validate(self, value: str) -> ValidationResult:
        error = self.fn(value)
        return self.success() if error is None else self.failure(error)


def _field(label: str, input_widget: Input) -> Vertical:
    return Vertical(Static(label, classes="field-label"), input_widget, classes="field")


class ScheduleRow(Horizontal):
    """One schedule: keep-it checkbox + retention count input."""

    def __init__(self, schedule: str, enabled: bool, retention: int):
        super().__init__(classes="schedule-row")
        self.schedule = schedule
        self._enabled = enabled
        self._retention = retention

    def compose(self) -> ComposeResult:
        yield Checkbox(f"keep {self.schedule}", value=self._enabled, classes="sched-on")
        yield Input(
            value=str(self._retention),
            classes="sched-retention",
            validators=[FnValidator(v.required_int)],
        )

    def value(self) -> Optional[dict]:
        if not self.query_one(".sched-on", Checkbox).value:
            return None
        return {"retention": int(self.query_one(".sched-retention", Input).value or 3)}


class NotifyUrlRow(Horizontal):
    """One apprise URL: input (validated through apprise itself) + remove."""

    def __init__(self):
        super().__init__(classes="notify-url-row")

    def compose(self) -> ComposeResult:
        yield Input(
            classes="notify-url",
            placeholder="ntfy://ntfy.sh/my-topic",
            validators=[FnValidator(v.apprise_url)],
        )
        yield Button("Remove", classes="remove-notify-url", variant="warning")

    def value(self) -> Optional[str]:
        return self.query_one(".notify-url", Input).value.strip() or None


class RemoteForm(Vertical):
    """Sub-form for one rclone backup remote; added/removed dynamically."""

    def __init__(self):
        super().__init__(classes="remote-form")

    def compose(self) -> ComposeResult:
        yield Static("Remote", classes="section-sub")
        yield _field("Short name (e.g. backblaze)", Input(classes="r-name", validators=[FnValidator(v.remote_name)]))
        yield _field(
            "rclone target (<remote>:<path>)",
            Input(
                classes="r-target",
                placeholder="b2-runtipi:my-bucket/runtipi-backups",
                validators=[FnValidator(v.rclone_target)],
            ),
        )
        yield _field(
            "Bandwidth limit (empty = none)",
            Input(classes="r-bwlimit", placeholder="5M"),
        )
        yield Static("Retention on this remote:", classes="field-label")
        for schedule in VALID_SCHEDULES:
            yield ScheduleRow(
                schedule,
                enabled=schedule in REMOTE_RETENTION_DEFAULTS,
                retention=REMOTE_RETENTION_DEFAULTS.get(schedule, 3),
            )
        yield Button("Remove this remote", classes="remove-remote", variant="warning")

    def value(self) -> Optional[dict]:
        name = self.query_one(".r-name", Input).value.strip()
        target = self.query_one(".r-target", Input).value.strip().rstrip("/")
        if not name or not target:
            return None
        schedules = {}
        for row in self.query(ScheduleRow):
            if (val := row.value()) is not None:
                schedules[row.schedule] = val
        if not schedules:
            schedules = {"daily": {"retention": REMOTE_RETENTION_DEFAULTS["daily"]}}
        bwlimit = self.query_one(".r-bwlimit", Input).value.strip() or None
        return {
            "name": name,
            "rclone_remote": target,
            "enabled": True,
            "bandwidth_limit": bwlimit,
            "schedules": schedules,
        }


class ConfigFormApp(App):
    """The whole config as one scrollable form. Returns the answers dict via
    App.exit(), or None when cancelled."""

    TITLE = "runtipi-companion config"
    BINDINGS = [("ctrl+s", "save", "Save"), ("escape", "cancel", "Cancel")]

    CSS = """
    #form { padding: 1 2; }
    .section { margin-top: 1; text-style: bold; color: $accent; }
    .section-sub { text-style: bold; }
    .field { height: auto; margin-bottom: 1; }
    .field-label { color: $text-muted; }
    .schedule-row { height: 3; }
    .sched-retention { width: 12; }
    .remote-form { border: round $secondary; padding: 0 1; margin-bottom: 1; height: auto; }
    #remotes { height: auto; }
    #security-custom { height: auto; }
    #notify-urls { height: auto; }
    .notify-url-row { height: 3; }
    .notify-url { width: 60; }
    #error-bar { color: $error; height: auto; min-height: 1; }
    #buttons { height: 3; margin-top: 1; }
    """

    def __init__(self, default_save_path: str):
        super().__init__()
        self.default_save_path = default_save_path

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="form"):
            yield Static("Runtipi install", classes="section")
            yield _field(
                "Path to runtipi install",
                Input(value="/opt/runtipi", id="runtipi-path", validators=[FnValidator(v.absolute_path)]),
            )
            yield _field(
                "runtipi-cli path (empty = auto-detect)",
                Input(id="cli-path", validators=[FnValidator(v.optional_absolute_path)]),
            )
            yield _field("App ids to manage, comma-separated (empty = all)", Input(id="apps"))

            yield Static("Backups", classes="section")
            yield _field(
                "Local backup directory (empty = <install>/backups)",
                Input(id="local-path", validators=[FnValidator(v.optional_absolute_path)]),
            )
            yield _field("Host label on remotes (empty = hostname)", Input(id="host-label"))
            yield _field(
                "Scratch directory",
                Input(value="/tmp/runtipi-companion", id="work-dir", validators=[FnValidator(v.absolute_path)]),
            )
            yield Checkbox("Stop apps while backing them up (safer, brief downtime)", value=True, id="stop-apps")
            yield Static("Local retention:", classes="field-label")
            for schedule in VALID_SCHEDULES:
                yield ScheduleRow(schedule, enabled=True, retention=LOCAL_RETENTION_DEFAULTS.get(schedule, 3))

            yield Static("Backup remotes (rclone)", classes="section")
            yield Vertical(id="remotes")
            yield Button("Add a remote", id="add-remote", variant="primary")

            yield Static("Security hardening defaults", classes="section")
            yield Select(
                [("Recommended (key-only SSH, no root login, UFW, fail2ban)", "recommended"), ("Custom", "custom")],
                value="recommended",
                id="security-preset",
                allow_blank=False,
            )
            with Vertical(id="security-custom"):
                yield Checkbox("Disable SSH password auth", value=True, id="ssh-nopass")
                yield Checkbox("Disable SSH root login", value=True, id="ssh-noroot")
                yield _field(
                    "Custom SSH port (empty = keep current)",
                    Input(id="ssh-port", validators=[FnValidator(v.optional_port)]),
                )
                yield Checkbox("Enable UFW firewall", value=True, id="ufw-on")
                yield _field(
                    "UFW allowed TCP ports", Input(value="22", id="ufw-ports", validators=[FnValidator(v.csv_ports)])
                )
                yield Checkbox("Enable fail2ban", value=True, id="f2b-on")
                yield _field(
                    "fail2ban max retries", Input(value="3", id="f2b-retries", validators=[FnValidator(v.required_int)])
                )
                yield _field(
                    "fail2ban ban time (seconds)",
                    Input(
                        value="3600",
                        id="f2b-bantime",
                        validators=[FnValidator(lambda x: v.required_int(x, 1, 10_000_000))],
                    ),
                )

            yield Static("Tailscale", classes="section")
            yield Checkbox("Set up Tailscale for private remote access", value=False, id="ts-on")
            yield Checkbox("Tailscale-only lockdown (VPN-only, cuts public access)", value=False, id="ts-only")
            yield Checkbox("Advertise as exit node", value=False, id="ts-exit")
            yield Checkbox("Enable Tailscale SSH", value=False, id="ts-ssh")

            yield Static("Notifications", classes="section")
            yield Static("Apprise URLs (one per row, validated live)", classes="field-label")
            yield Vertical(id="notify-urls")
            yield Button("Add notification URL", id="add-notify-url", variant="primary")
            yield Checkbox("Notify on successful backups too", value=False, id="notify-success")

            yield Static("Save", classes="section")
            yield _field(
                "Config file path",
                Input(value=self.default_save_path, id="save-path", validators=[FnValidator(v.absolute_path)]),
            )
            yield Static("", id="error-bar")
            with Horizontal(id="buttons"):
                yield Button("Save (ctrl+s)", id="save", variant="success")
                yield Button("Cancel (esc)", id="cancel")
        yield Footer()

    # -- events --------------------------------------------------------

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "security-preset":
            self.query_one("#security-custom").display = event.value == "custom"

    def on_input_changed(self, event: Input.Changed) -> None:
        # Realtime feedback: surface the failing field's message immediately.
        bar = self.query_one("#error-bar", Static)
        if event.validation_result is not None and not event.validation_result.is_valid:
            bar.update(event.validation_result.failure_descriptions[0])
        else:
            bar.update("")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "add-remote":
            self.query_one("#remotes", Vertical).mount(RemoteForm())
        elif event.button.id == "add-notify-url":
            self.query_one("#notify-urls", Vertical).mount(NotifyUrlRow())
        elif event.button.has_class("remove-remote") or event.button.has_class("remove-notify-url"):
            target_type = RemoteForm if event.button.has_class("remove-remote") else NotifyUrlRow
            node = event.button.parent
            while node is not None and not isinstance(node, target_type):
                node = node.parent
            if node is not None:
                node.remove()
        elif event.button.id == "save":
            self.action_save()
        elif event.button.id == "cancel":
            self.action_cancel()

    def action_cancel(self) -> None:
        self.exit(None)

    def action_save(self) -> None:
        first_invalid = self._first_invalid_input()
        if first_invalid is not None:
            result = first_invalid.validate(first_invalid.value)
            message = result.failure_descriptions[0] if result and not result.is_valid else "Invalid value."
            self.query_one("#error-bar", Static).update(message)
            first_invalid.focus()
            return
        self.exit(self._collect())

    # -- helpers -------------------------------------------------------

    def _first_invalid_input(self) -> Optional[Input]:
        skip_custom_security = self.query_one("#security-preset", Select).value == "recommended"
        for input_widget in self.query(Input):
            if not input_widget.validators:
                continue
            # Custom-security fields don't count while the recommended
            # preset is selected (they're hidden and unused).
            if skip_custom_security and any(a.id == "security-custom" for a in input_widget.ancestors):
                continue
            result = input_widget.validate(input_widget.value)
            if result is not None and not result.is_valid:
                return input_widget
        return None

    def _text(self, widget_id: str) -> str:
        return self.query_one(f"#{widget_id}", Input).value.strip()

    def _checked(self, widget_id: str) -> bool:
        return self.query_one(f"#{widget_id}", Checkbox).value

    def _collect(self) -> dict:
        local_schedules = {}
        for row in self.query(ScheduleRow):
            if any(isinstance(a, RemoteForm) for a in row.ancestors):
                continue
            if (val := row.value()) is not None:
                local_schedules[row.schedule] = val
        remotes = [val for form in self.query(RemoteForm) if (val := form.value()) is not None]

        if self.query_one("#security-preset", Select).value == "recommended":
            security = {
                "ssh": {"disable_password_auth": True, "disable_root_login": True, "port": None},
                "ufw": {"enable": True, "allowed_tcp_ports": [22]},
                "fail2ban": {"enabled": True, "maxretry": 3, "bantime": 3600},
            }
        else:
            security = {
                "ssh": {
                    "disable_password_auth": self._checked("ssh-nopass"),
                    "disable_root_login": self._checked("ssh-noroot"),
                    "port": int(self._text("ssh-port")) if self._text("ssh-port") else None,
                },
                "ufw": {
                    "enable": self._checked("ufw-on"),
                    "allowed_tcp_ports": [int(p.strip()) for p in self._text("ufw-ports").split(",") if p.strip()],
                },
                "fail2ban": {
                    "enabled": self._checked("f2b-on"),
                    "maxretry": int(self._text("f2b-retries") or 3),
                    "bantime": int(self._text("f2b-bantime") or 3600),
                },
            }
        ts_on = self._checked("ts-on")
        ts_only = ts_on and self._checked("ts-only")
        security["tailscale_only"] = {
            "enabled": ts_only,
            "tailscale_ssh": ts_only,
            "tailscale_port_udp": 41641,
        }

        urls = [url for row in self.query(NotifyUrlRow) if (url := row.value()) is not None]
        return {
            "version": CONFIG_VERSION,
            "runtipi": {
                "path": self._text("runtipi-path"),
                "cli_path": self._text("cli-path") or None,
                "apps": [a.strip() for a in self._text("apps").split(",") if a.strip()],
            },
            "backup": {
                "work_dir": self._text("work-dir"),
                "local_path": self._text("local-path") or None,
                "host_label": self._text("host-label") or None,
                "stop_apps": self._checked("stop-apps"),
                "sleep_duration": 10,
                "schedules": local_schedules,
                "remotes": remotes,
            },
            "security": security,
            "tailscale": {
                "enabled": ts_on,
                "auth_key_env": "TAILSCALE_AUTHKEY",
                "advertise_exit_node": ts_on and self._checked("ts-exit"),
                "ssh": ts_on and (ts_only or self._checked("ts-ssh")),
            },
            "updates": {
                "auto_update_core": False,
                "auto_update_apps": False,
                "exclude_apps": [],
                "backup_before": True,
            },
            "notify": {
                "urls": urls,
                "webhook_url": None,
                "notify_on_success": self._checked("notify-success"),
                "notify_on_failure": True,
            },
            "_save_path": self._text("save-path"),
        }

    def on_mount(self) -> None:
        self.query_one("#security-custom").display = False
        self.query_one("#remotes", Vertical)


def run_form_wizard(path: Optional[str] = None) -> Optional[Path]:
    """Run the form; write through config_wizard.write_config (same
    round-trip validation as the classic wizard). Returns the written path,
    or None when cancelled."""
    default_dest = path or str(cw.default_config_path())
    answers = ConfigFormApp(default_dest).run()
    if answers is None:
        console.print("[yellow]Aborted -- nothing written.[/yellow]")
        return None

    dest = Path(answers.pop("_save_path"))
    if dest.exists() and not cw._ask_bool(f"{dest} already exists. Overwrite?", default=False):
        console.print("[yellow]Aborted -- nothing written.[/yellow]")
        return None
    cw.write_config(answers, dest)
    console.print(f"[green]Config written to {dest} and validated.[/green]")
    console.print("Next: [bold]runtipi-companion setup --apply[/bold] bootstraps the system itself.")
    return dest


def wizard_available() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()
