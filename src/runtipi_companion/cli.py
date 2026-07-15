from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from . import backup as backup_mod
from . import config_wizard
from . import restore as restore_mod
from . import security as security_mod
from . import setup_wizard
from . import tailscale as tailscale_mod
from . import update as update_mod
from .config import DEFAULT_CONFIG_PATHS, CompanionConfig, ConfigError, load_config
from .notify import notify
from .templates import EXAMPLE_CONFIG

console = Console()

app = typer.Typer(
    help="Companion CLI for Runtipi: multi-remote backups, restores, updates, "
    "VPS security hardening, and Tailscale setup.",
    no_args_is_help=True,
)

config_app = typer.Typer(help="Manage runtipi-companion configuration.", no_args_is_help=True)
backup_app = typer.Typer(help="Create and manage app backups (local + rclone remotes).", no_args_is_help=True)
restore_app = typer.Typer(help="Restore apps from a backup.", no_args_is_help=True)
update_app = typer.Typer(help="Update apps, app stores, or Runtipi core.", no_args_is_help=True)
security_app = typer.Typer(help="VPS/server hardening (SSH, UFW, fail2ban).", no_args_is_help=True)
tailscale_app = typer.Typer(help="Install and configure Tailscale.", no_args_is_help=True)
setup_app = typer.Typer(help="First-run setup wizard.", no_args_is_help=True)

app.add_typer(config_app, name="config")
app.add_typer(backup_app, name="backup")
app.add_typer(restore_app, name="restore")
app.add_typer(update_app, name="update")
app.add_typer(security_app, name="security")
app.add_typer(tailscale_app, name="tailscale")
app.add_typer(setup_app, name="setup")


def _load(config_path: Optional[str]) -> CompanionConfig:
    try:
        return load_config(config_path)
    except ConfigError as e:
        # First run on an interactive terminal: offer to build the config
        # right here instead of erroring out. Non-interactive callers
        # (cron, systemd, CI) still get the plain error.
        if config_path is None and sys.stdin.isatty() and not any(p.exists() for p in DEFAULT_CONFIG_PATHS):
            console.print("[yellow]No config file found -- looks like a first run.[/yellow]")
            if typer.confirm("Run the config setup wizard now?", default=True):
                written = config_wizard.run_config_wizard()
                if written is not None:
                    return load_config(str(written))
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)


ConfigOption = typer.Option(None, "--config", "-c", help="Path to config file.")
# Everything that can change system state defaults to a dry-run preview;
# pass --apply to actually execute. This applies uniformly (backups,
# restores, updates, security, tailscale) so behavior is predictable.
DryRunOption = typer.Option(True, "--dry-run/--apply", help="Preview changes without applying them.")
YesOption = typer.Option(False, "--yes", "-y", help="Assume yes on confirmation prompts.")


# ---- config ----


@config_app.command("init")
def config_init(
    path: str = typer.Option(str(DEFAULT_CONFIG_PATHS[1]), "--path", help="Where to write the new config file."),
    force: bool = typer.Option(False, "--force", help="Overwrite if it already exists."),
):
    """Write an example config file to get started."""
    dest = Path(path)
    if dest.exists() and not force:
        console.print(f"[red]{dest} already exists. Use --force to overwrite.[/red]")
        raise typer.Exit(1)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(EXAMPLE_CONFIG)
    console.print(f"[green]Wrote example config to {dest}[/green]")
    console.print(f"Edit it, then run any command with --config {dest} (or move it to a default location).")


@config_app.command("wizard")
def config_wizard_cmd(
    path: Optional[str] = typer.Option(None, "--path", help="Where to write the config file (default: asked interactively)."),
):
    """Interactive wizard that builds and writes a config file. Also offered
    automatically on first run when no config exists."""
    written = config_wizard.run_config_wizard(path)
    if written is None:
        raise typer.Exit(1)


@config_app.command("show")
def config_show(config: Optional[str] = ConfigOption):
    """Print the fully-resolved config (after defaults) as JSON."""
    cfg = _load(config)
    console.print_json(json.dumps(dataclasses.asdict(cfg), default=str, indent=2))


@config_app.command("validate")
def config_validate(config: Optional[str] = ConfigOption):
    _load(config)
    console.print("[green]Config is valid.[/green]")


# ---- backup ----


@backup_app.command("run")
def backup_run(
    schedule: str = typer.Option("daily", "--type", help="daily|weekly|monthly|yearly"),
    apps: Optional[str] = typer.Option(None, "--apps", help="Comma-separated app ids (default: all configured/installed)."),
    remotes: Optional[str] = typer.Option(None, "--remote", help="Comma-separated remote names to sync to (default: all enabled)."),
    local_only: bool = typer.Option(False, "--local-only", help="Skip syncing to remotes."),
    stop: Optional[bool] = typer.Option(None, "--stop/--no-stop", help="Override backup.stop_apps."),
    config: Optional[str] = ConfigOption,
    dry_run: bool = DryRunOption,
):
    """Create a backup for the given schedule and prune according to each
    location's (local + every synced remote) retention policy."""
    cfg = _load(config)
    app_list = apps.split(",") if apps else None
    remote_list = remotes.split(",") if remotes else None
    try:
        created = backup_mod.run_backup(
            cfg,
            schedule,
            apps=app_list,
            stop_apps=stop,
            remotes=remote_list,
            local_only=local_only,
            dry_run=dry_run,
        )
        if not dry_run:
            notify(cfg.notify, f"runtipi-companion: {schedule} backup completed ({len(created)} archives)", success=True)
    except Exception as e:
        if not dry_run:
            notify(cfg.notify, f"runtipi-companion: {schedule} backup FAILED: {e}", success=False)
        raise


@backup_app.command("list")
def backup_list(
    app: str = typer.Argument(..., help="App id to list backups for."),
    remote: Optional[str] = typer.Option(None, help="List backups on this remote instead of locally."),
    config: Optional[str] = ConfigOption,
):
    cfg = _load(config)
    if remote:
        files = restore_mod.list_remote_backups(cfg, remote, app)
    else:
        files = [str(p) for p in restore_mod.list_local_backups(cfg, app)]
    if not files:
        console.print("No backups found.")
    for f in files:
        console.print(f)


# ---- restore ----


@restore_app.command("run")
def restore_run(
    app_id: str = typer.Argument(...),
    backup_file: str = typer.Argument(..., help="Filename as shown by 'backup list' (or 'restore list')."),
    store: str = typer.Option("migrated", "--store", help="App store name (see 'runtipi-cli installed')."),
    from_remote: Optional[str] = typer.Option(None, "--from-remote", help="Download from this remote first."),
    config: Optional[str] = ConfigOption,
    yes: bool = YesOption,
    dry_run: bool = DryRunOption,
):
    cfg = _load(config)
    restore_mod.restore_backup(
        cfg, store, app_id, backup_file, from_remote=from_remote, assume_yes=yes, dry_run=dry_run
    )


@restore_app.command("list")
def restore_list(
    app_id: str = typer.Argument(...),
    remote: Optional[str] = typer.Option(None),
    config: Optional[str] = ConfigOption,
):
    backup_list(app_id, remote, config)


# ---- update ----


@update_app.command("apps")
def update_apps_cmd(
    apps: Optional[str] = typer.Option(None, "--apps", help="Comma-separated app ids (default: all)."),
    config: Optional[str] = ConfigOption,
    dry_run: bool = DryRunOption,
):
    cfg = _load(config)
    app_list = apps.split(",") if apps else None
    update_mod.update_apps(cfg, apps=app_list, dry_run=dry_run)


@update_app.command("core")
def update_core_cmd(
    version: str = typer.Option("latest"),
    config: Optional[str] = ConfigOption,
    dry_run: bool = DryRunOption,
):
    cfg = _load(config)
    update_mod.update_core(cfg, version, dry_run=dry_run)


@update_app.command("appstores")
def update_appstores_cmd(config: Optional[str] = ConfigOption, dry_run: bool = DryRunOption):
    cfg = _load(config)
    update_mod.update_appstores(cfg, dry_run=dry_run)


# ---- security ----


@security_app.command("harden")
def security_harden(
    ssh: bool = typer.Option(False, "--ssh"),
    ufw: bool = typer.Option(False, "--ufw"),
    fail2ban: bool = typer.Option(False, "--fail2ban"),
    all_: bool = typer.Option(False, "--all"),
    force: bool = typer.Option(False, "--force", help="Skip the authorized_keys safety check for SSH hardening."),
    config: Optional[str] = ConfigOption,
    yes: bool = YesOption,
    dry_run: bool = DryRunOption,
):
    """Apply the runtipi VPS-security / server-hardening checklist. Defaults
    to a dry-run preview -- pass --apply to actually change sshd_config,
    firewall rules, or install fail2ban."""
    cfg = _load(config)
    if not any([ssh, ufw, fail2ban, all_]):
        console.print("Nothing selected. Pass --ssh, --ufw, --fail2ban, or --all.")
        raise typer.Exit(1)
    if ssh or all_:
        security_mod.harden_ssh(cfg, dry_run=dry_run, assume_yes=yes, force=force)
    if ufw or all_:
        security_mod.harden_ufw(cfg, dry_run=dry_run, assume_yes=yes)
    if fail2ban or all_:
        security_mod.harden_fail2ban(cfg, dry_run=dry_run, assume_yes=yes)


@security_app.command("status")
def security_status(config: Optional[str] = ConfigOption):
    cfg = _load(config)
    security_mod.status(cfg)


# ---- tailscale ----


@tailscale_app.command("install")
def tailscale_install(config: Optional[str] = ConfigOption, yes: bool = YesOption, dry_run: bool = DryRunOption):
    cfg = _load(config)
    tailscale_mod.install_tailscale(cfg, dry_run=dry_run, assume_yes=yes)


@tailscale_app.command("status")
def tailscale_status():
    tailscale_mod.status()


# ---- setup ----


@setup_app.command("wizard")
def setup_wizard_cmd(config: Optional[str] = ConfigOption, yes: bool = YesOption, dry_run: bool = DryRunOption):
    cfg = _load(config)
    setup_wizard.run_wizard(cfg, dry_run=dry_run, assume_yes=yes)


if __name__ == "__main__":
    app()
