"""One-shot health audit: report pass/warn/fail against the setup and the
VPS-security checklist instead of applying changes (the read-only sibling of
`security harden` and `setup wizard`).

Check helpers that parse command output are kept pure (take strings, return
results) so they're unit-testable without root or a real box.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.table import Table

from . import __version__
from .backup.rclone import RcloneClient
from .config import CompanionConfig
from .system import version_check
from .system.runtipi_cli import RuntipiCLI, RuntipiCLIError
from .system.shell import run

console = Console()

OK = "ok"
WARN = "warn"
FAIL = "fail"

_STATUS_STYLE = {OK: "[green]PASS[/green]", WARN: "[yellow]WARN[/yellow]", FAIL: "[red]FAIL[/red]"}

# A daily timer that hasn't produced a backup in this long is worth flagging.
STALE_BACKUP_AGE = 2 * 24 * 60 * 60


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str = ""


def evaluate_sshd_config(sshd_t_output: str, cfg: CompanionConfig) -> list:
    """Compare `sshd -T` effective values against what the config says
    hardening should have applied.
    """
    effective = {}
    for line in sshd_t_output.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            effective[parts[0].lower()] = parts[1].lower()

    results = []
    if cfg.security.ssh.disable_password_auth:
        value = effective.get("passwordauthentication")
        results.append(
            CheckResult(
                "ssh: password authentication disabled",
                OK if value == "no" else FAIL,
                f"passwordauthentication {value or 'unknown'}",
            )
        )
    if cfg.security.ssh.disable_root_login:
        value = effective.get("permitrootlogin")
        results.append(
            CheckResult(
                "ssh: root login disabled",
                OK if value in ("no", "prohibit-password") else FAIL,
                f"permitrootlogin {value or 'unknown'}",
            )
        )
    if cfg.security.ssh.port:
        value = effective.get("port")
        results.append(
            CheckResult(
                f"ssh: port is {cfg.security.ssh.port}",
                OK if value == str(cfg.security.ssh.port) else FAIL,
                f"port {value or 'unknown'}",
            )
        )
    return results


def newest_backup_age(backup_root: Path) -> Optional[float]:
    """Age in seconds of the most recent archive under backup_root, or None
    if there are no archives at all."""
    newest = None
    for archive in backup_root.rglob("*.tar.gz"):
        try:
            mtime = archive.stat().st_mtime
        except OSError:
            continue
        if newest is None or mtime > newest:
            newest = mtime
    return None if newest is None else time.time() - newest


def _check_runtipi(cfg: CompanionConfig) -> list:
    results = []
    path = Path(cfg.runtipi.path)
    if path.is_dir():
        results.append(CheckResult("runtipi install directory", OK, str(path)))
    else:
        results.append(CheckResult("runtipi install directory", FAIL, f"{path} not found"))
        return results  # everything below depends on the install

    try:
        cli = RuntipiCLI(cfg.runtipi.path, cfg.runtipi.cli_path, dry_run=True)
        results.append(CheckResult("runtipi-cli", OK, cli.cli_path))
    except RuntipiCLIError:
        results.append(CheckResult("runtipi-cli", FAIL, "not found (set runtipi.cli_path)"))

    docker = run(["docker", "info"], check=False, quiet=True)
    results.append(
        CheckResult(
            "docker daemon",
            OK if docker.ok else FAIL,
            "reachable" if docker.ok else "docker info failed (daemon down, or user not in docker group)",
        )
    )
    return results


def _check_backups(cfg: CompanionConfig) -> list:
    results = []
    backup_root = Path(cfg.backup_local_path)
    if not backup_root.is_dir():
        results.append(CheckResult("backup directory", WARN, f"{backup_root} missing (created on first backup)"))
        return results
    if not os.access(backup_root, os.W_OK):
        results.append(CheckResult("backup directory", FAIL, f"{backup_root} not writable"))
        return results
    results.append(CheckResult("backup directory", OK, str(backup_root)))

    age = newest_backup_age(backup_root)
    if age is None:
        results.append(CheckResult("recent backup exists", WARN, "no archives yet"))
    elif age > STALE_BACKUP_AGE:
        results.append(
            CheckResult(
                "recent backup exists",
                WARN,
                f"newest archive is {age / 86400:.1f} days old -- is the timer/cron running?",
            )
        )
    else:
        results.append(CheckResult("recent backup exists", OK, f"newest is {age / 3600:.1f}h old"))

    timer = run(["systemctl", "is-active", "runtipi-companion-backup-daily.timer"], check=False, quiet=True)
    results.append(
        CheckResult(
            "daily backup timer",
            OK if timer.ok else WARN,
            "active" if timer.ok else "inactive (fine if you use cron instead)",
        )
    )
    return results


def _check_remotes(cfg: CompanionConfig) -> list:
    enabled = [r for r in cfg.backup.remotes if r.enabled]
    if not enabled:
        return []
    rclone = RcloneClient(dry_run=True)
    if not rclone.is_installed():
        return [CheckResult("rclone", FAIL, "remotes configured but rclone is not installed")]
    results = [CheckResult("rclone", OK, "installed")]
    configured = set(rclone.list_remotes())
    for remote in enabled:
        remote_name = remote.rclone_remote.split(":")[0]
        results.append(
            CheckResult(
                f"rclone remote '{remote_name}'",
                OK if remote_name in configured else FAIL,
                "configured" if remote_name in configured else "unknown to rclone -- run 'rclone config'",
            )
        )
    return results


def _check_security(cfg: CompanionConfig) -> list:
    results = []

    sshd = run(["sshd", "-T"], sudo=True, check=False, quiet=True)
    if sshd.ok:
        results.extend(evaluate_sshd_config(sshd.stdout, cfg))
    else:
        results.append(CheckResult("sshd effective config", WARN, "could not run 'sshd -T' (needs sudo)"))

    if cfg.security.ufw.enable:
        ufw = run(["ufw", "status"], sudo=True, check=False, quiet=True)
        active = ufw.ok and "Status: active" in ufw.stdout
        results.append(
            CheckResult(
                "ufw firewall active",
                OK if active else FAIL,
                "active" if active else "inactive or not installed",
            )
        )

    if cfg.security.fail2ban.enabled:
        f2b = run(["systemctl", "is-active", "fail2ban"], check=False, quiet=True)
        results.append(
            CheckResult(
                "fail2ban active",
                OK if f2b.ok else FAIL,
                "active" if f2b.ok else "inactive or not installed",
            )
        )

    if cfg.tailscale.enabled or cfg.security.tailscale_only.enabled:
        if shutil.which("tailscale") is None:
            results.append(CheckResult("tailscale", FAIL, "enabled in config but not installed"))
        else:
            ts = run(["tailscale", "status"], check=False, quiet=True)
            results.append(
                CheckResult(
                    "tailscale up",
                    OK if ts.ok else FAIL,
                    "connected" if ts.ok else "installed but not up ('tailscale up')",
                )
            )
    return results


def _check_notify(cfg: CompanionConfig) -> list:
    if not cfg.notify.urls:
        return []
    import apprise

    results = []
    for url in cfg.notify.urls:
        valid = apprise.Apprise().add(url)
        results.append(
            CheckResult(
                "notify URL parses",
                OK if valid else FAIL,
                url if valid else f"apprise rejected: {url}",
            )
        )
    return results


def _check_version() -> list:
    latest = version_check.check_for_update(force=True)
    if latest:
        return [CheckResult("runtipi-companion up to date", WARN, f"{__version__} installed, {latest} available")]
    return [CheckResult("runtipi-companion up to date", OK, __version__)]


def _check_config_version(cfg: CompanionConfig) -> list:
    from .config import CONFIG_VERSION

    if cfg.version < CONFIG_VERSION:
        return [
            CheckResult(
                "config schema version",
                WARN,
                f"v{cfg.version} (current: v{CONFIG_VERSION}) -- run 'config migrate --apply'",
            )
        ]
    return [CheckResult("config schema version", OK, f"v{cfg.version}")]


def run_doctor(cfg: CompanionConfig) -> list:
    results = []
    results.extend(_check_config_version(cfg))
    results.extend(_check_runtipi(cfg))
    results.extend(_check_backups(cfg))
    results.extend(_check_remotes(cfg))
    results.extend(_check_security(cfg))
    results.extend(_check_notify(cfg))
    results.extend(_check_version())
    return results


def render(results: list) -> bool:
    """Print the report table. Returns True if any check failed."""
    table = Table(title="runtipi-companion doctor")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail", overflow="fold")
    for r in results:
        table.add_row(r.name, _STATUS_STYLE[r.status], r.detail)
    console.print(table)

    fails = sum(1 for r in results if r.status == FAIL)
    warns = sum(1 for r in results if r.status == WARN)
    if fails:
        console.print(f"[red]{fails} check(s) failed[/red], {warns} warning(s).")
    elif warns:
        console.print(f"[green]No failures[/green], {warns} warning(s).")
    else:
        console.print("[green]All checks passed.[/green]")
    return fails > 0
