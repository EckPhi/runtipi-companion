"""Interactive pickers for commands whose arguments are discoverable:
instead of making the user type app ids and archive filenames, list what
actually exists (locally or on a remote) and let them choose.

Prompting goes through config_wizard's _ask* seams so tests can script
every interactive flow the same way.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from rich.console import Console

from . import config_wizard as cw
from .config import CompanionConfig
from .rclone import RcloneClient

console = Console()


@dataclass
class RestoreSelection:
    store: str
    app_id: str
    backup_file: str  # filename for local, remote-relative path for remotes
    from_remote: Optional[str] = None


def multi_select(prompt: str, options: list) -> list:
    """Numbered checklist. User enters comma-separated numbers, 'all', or
    empty for none. Returns the selected indices, sorted."""
    for i, opt in enumerate(options, 1):
        console.print(f"  {i}. {opt}")
    raw = cw._ask(f"{prompt} (comma-separated numbers, 'all', or empty for none)", default="")
    raw = raw.strip().lower()
    if raw in ("all", "a"):
        return list(range(len(options)))
    if not raw:
        return []
    indices = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            n = int(part)
        except ValueError:
            console.print(f"[red]Ignoring '{part}' -- not a number.[/red]")
            continue
        if 1 <= n <= len(options):
            indices.add(n - 1)
        else:
            console.print(f"[red]Ignoring {n} -- out of range.[/red]")
    return sorted(indices)


HARDENING_ITEMS = [
    ("ssh", "SSH (disable password auth / root login, optional port change)"),
    ("ufw", "UFW firewall (allow configured ports, default deny)"),
    ("fail2ban", "fail2ban (ban repeated SSH auth failures)"),
    ("tailscale_security", "Tailscale-only access (VPN-only lockdown: tailscale ssh + ufw allow-tailscale0-only)"),
]


def select_hardening() -> dict:
    """Interactive checklist for `security harden`. Returns {key: bool} for
    every item in HARDENING_ITEMS."""
    console.print("[bold]Select what to harden[/bold]")
    indices = multi_select("Toggle items to harden", [label for _, label in HARDENING_ITEMS])
    return {key: (i in indices) for i, (key, _) in enumerate(HARDENING_ITEMS)}


def pick(prompt: str, options: list) -> int:
    """Numbered menu. Single option is auto-selected."""
    if len(options) == 1:
        console.print(f"{prompt}: [bold]{options[0]}[/bold] (only option)")
        return 0
    for i, opt in enumerate(options, 1):
        console.print(f"  {i}. {opt}")
    while True:
        choice = cw._ask_int(prompt, 1)
        if 1 <= choice <= len(options):
            return choice - 1
        console.print(f"[red]Enter a number between 1 and {len(options)}.[/red]")


def _pick_app_and_file(archives: dict) -> tuple:
    """archives: {(store, app_id): [file, ...]} -> ((store, app_id), file)"""
    apps = sorted(archives)
    idx = pick("Which app to restore", [f"{app}:{store}" for store, app in apps])
    store, app_id = apps[idx]
    files = sorted(archives[(store, app_id)], reverse=True)  # newest first
    file_idx = pick("Which backup", files)
    return (store, app_id), files[file_idx]


def interactive_restore(cfg: CompanionConfig) -> Optional[RestoreSelection]:
    """Walk the user from 'I want to restore something' to a concrete
    (store, app, archive, source). Returns None when there is nothing to
    restore from the chosen source."""
    console.print("[bold]Restore an app from a backup[/bold]")

    sources = ["local disk"] + [f"remote '{r.name}'" for r in cfg.backup.remotes]
    source_idx = pick("Restore from", sources)

    if source_idx == 0:
        root = Path(cfg.backup_local_path)
        found = sorted(root.glob("*/*/*.tar.gz"))
        if not found:
            console.print(f"[yellow]No local backups under {root}.[/yellow]")
            return None
        archives = {}
        for p in found:
            # <root>/<store>/<app>/<app>-<schedule>-<date>.tar.gz
            archives.setdefault((p.parent.parent.name, p.parent.name), []).append(p.name)
        (store, app_id), filename = _pick_app_and_file(archives)
        return RestoreSelection(store=store, app_id=app_id, backup_file=filename)

    remote = cfg.backup.remotes[source_idx - 1]
    files = [f for f in RcloneClient().list_files(remote.rclone_remote) if f.endswith(".tar.gz")]
    if not files:
        console.print(f"[yellow]No backups found on remote '{remote.name}'.[/yellow]")
        return None
    archives = {}
    for f in files:
        parts = Path(f).parts
        if len(parts) < 3:
            continue  # not in <store>/<app>/<file> layout, skip
        archives.setdefault((parts[0], parts[1]), []).append(f)
    if not archives:
        console.print(f"[yellow]Remote '{remote.name}' has no backups in the expected store/app layout.[/yellow]")
        return None
    (store, app_id), remote_path = _pick_app_and_file(archives)
    return RestoreSelection(store=store, app_id=app_id, backup_file=remote_path, from_remote=remote.name)
