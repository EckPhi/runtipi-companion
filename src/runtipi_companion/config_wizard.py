"""Interactive first-run wizard that creates a config file.

This is distinct from setup_wizard.py: that one bootstraps a *system*
(clone runtipi, start it, check rclone) and requires a config to exist.
This one interviews the user and writes the config itself. The CLI offers
it automatically when no config file is found on an interactive terminal.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.syntax import Syntax

from .config import DEFAULT_CONFIG_PATHS, VALID_SCHEDULES, load_config

console = Console()

# Local retention defaults mirror the example config, not the dataclass
# defaults -- the wizard should suggest what we document as sensible.
LOCAL_RETENTION_DEFAULTS = {"daily": 7, "weekly": 4, "monthly": 6, "yearly": 2}
REMOTE_RETENTION_DEFAULTS = {"daily": 14, "weekly": 8, "monthly": 12, "yearly": 2}


# Thin wrappers so tests can monkeypatch a scripted answer queue without
# fighting rich internals.
def _ask(prompt: str, default: Optional[str] = None) -> str:
    if default is None:
        return Prompt.ask(prompt, console=console)
    return Prompt.ask(prompt, default=default, console=console)


def _ask_bool(prompt: str, default: bool = True) -> bool:
    return Confirm.ask(prompt, default=default, console=console)


def _ask_int(prompt: str, default: int) -> int:
    return IntPrompt.ask(prompt, default=default, console=console)


def _csv_list(raw: str) -> list:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _or_none(raw: str) -> Optional[str]:
    raw = raw.strip()
    return raw or None


def default_config_path() -> Path:
    """/etc for root (matches the systemd units), per-user config otherwise."""
    if os.geteuid() == 0:
        return DEFAULT_CONFIG_PATHS[0]
    return DEFAULT_CONFIG_PATHS[1]


def _prompt_schedules(defaults: dict, subject: str) -> dict:
    schedules = {}
    for name in VALID_SCHEDULES:
        if _ask_bool(f"  Keep [bold]{name}[/bold] backups {subject}?", default=name in defaults):
            retention = _ask_int(f"    How many {name} archives to keep", defaults.get(name, 3))
            schedules[name] = {"retention": retention}
    return schedules


def _prompt_remotes() -> list:
    remotes = []
    seen = set()
    console.print(
        "\nRemotes are rclone remotes (e.g. Backblaze B2, Google Drive, SFTP). "
        "Each gets its own retention per schedule. Configure the rclone side "
        "separately with 'rclone config'."
    )
    while _ask_bool("Add an off-site backup remote?", default=not remotes):
        name = _ask("  Short name for this remote (e.g. backblaze)")
        if not name or name in seen:
            console.print("  [red]Name must be non-empty and unique.[/red]")
            continue
        rclone_remote = _ask('  rclone target (e.g. "b2-runtipi:my-bucket/runtipi-backups")')
        if not rclone_remote:
            console.print("  [red]rclone target is required, skipping this remote.[/red]")
            continue
        bandwidth = _or_none(_ask('  Upload bandwidth limit (e.g. "5M", empty for none)', default=""))
        schedules = _prompt_schedules(REMOTE_RETENTION_DEFAULTS, f"on '{name}'")
        if not schedules:
            # validate_config rejects remotes without schedules; don't let the
            # wizard produce a config that fails its own validation.
            console.print("  [yellow]A remote needs at least one schedule -- keeping daily.[/yellow]")
            schedules = {"daily": {"retention": REMOTE_RETENTION_DEFAULTS["daily"]}}
        seen.add(name)
        remotes.append(
            {
                "name": name,
                "rclone_remote": rclone_remote,
                "enabled": True,
                "bandwidth_limit": bandwidth,
                "schedules": schedules,
            }
        )
    return remotes


def gather_answers() -> dict:
    """Interview the user and return a config dict shaped like the YAML file."""
    console.print(
        Panel.fit(
            "This wizard builds your runtipi-companion config file.\n"
            "Every question has a sensible default -- press Enter to accept it.",
            title="runtipi-companion config wizard",
        )
    )

    console.print("\n[bold]Runtipi install[/bold]")
    runtipi_path = _ask("Path to your runtipi install", default="/opt/runtipi")
    cli_path = _or_none(_ask("Path to runtipi-cli (empty = auto-detect inside the install)", default=""))
    apps = _csv_list(_ask("App ids to manage, comma-separated (empty = all installed apps)", default=""))

    console.print("\n[bold]Backups[/bold]")
    local_path = _or_none(_ask(f"Local backup directory (empty = {runtipi_path}/backups)", default=""))
    work_dir = _ask("Scratch directory for building archives", default="/tmp/runtipi-companion")
    stop_apps = _ask_bool("Stop apps while backing them up? (safer, brief downtime)", default=True)
    console.print("Local retention (how many archives to keep on this machine):")
    schedules = _prompt_schedules(LOCAL_RETENTION_DEFAULTS, "locally")
    remotes = _prompt_remotes()

    console.print("\n[bold]Security hardening defaults[/bold] (applied only when you run 'security harden')")
    if _ask_bool("Use recommended security defaults (key-only SSH, no root login, UFW, fail2ban)?", default=True):
        security = {
            "ssh": {"disable_password_auth": True, "disable_root_login": True, "port": None},
            "ufw": {"enable": True, "allowed_tcp_ports": [22]},
            "fail2ban": {"enabled": True, "maxretry": 3, "bantime": 3600},
        }
    else:
        ssh_port_raw = _or_none(_ask("Custom SSH port (empty = keep current)", default=""))
        ports = _csv_list(_ask("TCP ports UFW should allow, comma-separated", default="22"))
        security = {
            "ssh": {
                "disable_password_auth": _ask_bool("Disable SSH password auth?", default=True),
                "disable_root_login": _ask_bool("Disable SSH root login?", default=True),
                "port": int(ssh_port_raw) if ssh_port_raw else None,
            },
            "ufw": {
                "enable": _ask_bool("Enable UFW firewall?", default=True),
                "allowed_tcp_ports": [int(p) for p in ports] or [22],
            },
            "fail2ban": {
                "enabled": _ask_bool("Enable fail2ban?", default=True),
                "maxretry": _ask_int("fail2ban max retries before ban", 3),
                "bantime": _ask_int("fail2ban ban time in seconds", 3600),
            },
        }

    console.print("\n[bold]Tailscale[/bold]")
    ts_enabled = _ask_bool("Set up Tailscale for private remote access?", default=False)
    tailscale = {
        "enabled": ts_enabled,
        "auth_key_env": "TAILSCALE_AUTHKEY",
        "advertise_exit_node": ts_enabled and _ask_bool("Advertise this machine as an exit node?", default=False),
        "ssh": ts_enabled and _ask_bool("Enable Tailscale SSH?", default=False),
    }

    console.print("\n[bold]Notifications[/bold]")
    webhook = _or_none(_ask("Webhook URL for notifications (Discord/Slack/ntfy, empty = none)", default=""))
    notify = {
        "webhook_url": webhook,
        "notify_on_success": bool(webhook) and _ask_bool("Notify on successful backups too?", default=False),
        "notify_on_failure": True,
    }

    return {
        "runtipi": {"path": runtipi_path, "cli_path": cli_path, "apps": apps},
        "backup": {
            "work_dir": work_dir,
            "local_path": local_path,
            "stop_apps": stop_apps,
            "sleep_duration": 10,
            "schedules": schedules,
            "remotes": remotes,
        },
        "security": security,
        "tailscale": tailscale,
        "updates": {"auto_update_core": False, "auto_update_apps": False, "exclude_apps": []},
        "notify": notify,
    }


def write_config(answers: dict, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    header = "# runtipi-companion configuration (generated by the config wizard)\n# See README.md for documentation of every field.\n\n"
    dest.write_text(header + yaml.safe_dump(answers, sort_keys=False, default_flow_style=False))
    # Round-trip through the real loader so the wizard can never leave
    # behind a config the CLI later refuses to read.
    load_config(str(dest))
    return dest


def run_config_wizard(path: Optional[str] = None) -> Optional[Path]:
    """Run the full wizard. Returns the written path, or None if aborted."""
    answers = gather_answers()

    console.print("\n[bold]Review[/bold]")
    console.print(Syntax(yaml.safe_dump(answers, sort_keys=False), "yaml", background_color="default"))

    dest = Path(path) if path else Path(_ask("Where to save the config", default=str(default_config_path())))
    if dest.exists() and not _ask_bool(f"{dest} already exists. Overwrite?", default=False):
        console.print("[yellow]Aborted -- nothing written.[/yellow]")
        return None
    if not _ask_bool(f"Write config to {dest}?", default=True):
        console.print("[yellow]Aborted -- nothing written.[/yellow]")
        return None

    write_config(answers, dest)
    console.print(f"\n[green]Config written to {dest} and validated.[/green]")
    console.print("Next: run [bold]runtipi-companion setup wizard[/bold] to bootstrap the system itself.")
    return dest
