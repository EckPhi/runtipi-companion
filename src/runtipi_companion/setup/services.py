"""Install and enable the bundled systemd backup units (the packaged
replacement for hand-copying unit files, and the cron alternative from the
README). Timers get journalctl logging and Persistent=true: a run missed
because the box was off fires as soon as it's back up.
"""

from __future__ import annotations

import tempfile
from importlib import resources
from pathlib import Path

from rich.console import Console

from ..system.shell import confirm, run

console = Console()

UNIT_DIR = Path("/etc/systemd/system")
SERVICE_UNIT = "runtipi-companion-backup@.service"
VALID_SCHEDULES = ("daily", "weekly", "monthly", "yearly")
DEFAULT_SCHEDULES = ("daily", "weekly", "monthly")


def _unit_text(name: str) -> str:
    return resources.files("runtipi_companion").joinpath(f"data/systemd/{name}").read_text()


def _timer_unit(schedule: str) -> str:
    return f"runtipi-companion-backup-{schedule}.timer"


def install_services(schedules: list, *, dry_run: bool = True, assume_yes: bool = False) -> None:
    invalid = [s for s in schedules if s not in VALID_SCHEDULES]
    if invalid:
        raise ValueError(f"Unknown schedule(s) {invalid}, expected any of {list(VALID_SCHEDULES)}")

    units = [SERVICE_UNIT] + [_timer_unit(s) for s in schedules]
    console.print("[bold]systemd backup units plan[/bold]")
    for unit in units:
        console.print(f"  install {UNIT_DIR / unit}")
    for schedule in schedules:
        console.print(f"  systemctl enable --now {_timer_unit(schedule)}")

    if dry_run:
        console.print("[yellow]DRY-RUN[/yellow] -- no changes made. Re-run with --apply to install these.")
        return

    if not confirm("Install and enable these units?", assume_yes):
        console.print("Aborted.")
        return

    for unit in units:
        # Write the packaged unit somewhere we can read without privileges,
        # then move it into /etc/systemd/system as root.
        with tempfile.NamedTemporaryFile("w", suffix=unit.replace("/", "_"), delete=False) as tmp:
            tmp.write(_unit_text(unit))
            tmp_path = tmp.name
        run(["install", "-m", "0644", tmp_path, str(UNIT_DIR / unit)], sudo=True)
        Path(tmp_path).unlink(missing_ok=True)

    run(["systemctl", "daemon-reload"], sudo=True)
    for schedule in schedules:
        run(["systemctl", "enable", "--now", _timer_unit(schedule)], sudo=True)

    console.print("[green]Backup timers installed and enabled.[/green]")
    console.print("Check with: systemctl list-timers | grep runtipi-companion")
    console.print("Logs: journalctl -u runtipi-companion-backup@daily.service")
