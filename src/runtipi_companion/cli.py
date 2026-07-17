from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from . import __version__
from . import backup as backup_mod
from . import doctor as doctor_mod
from . import security as security_mod
from . import update as update_mod
from .backup import restore as restore_mod
from .config import DEFAULT_CONFIG_PATHS, CompanionConfig, ConfigError, load_config
from .config.templates import EXAMPLE_CONFIG
from .security import tailscale as tailscale_mod
from .setup import rclone as rclone_setup
from .setup import services as services_mod
from .setup import wizard as setup_wizard
from .system import version_check
from .system.notify import notify
from .system.shell import run as shell_run
from .ui import config_wizard, tui

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
setup_app = typer.Typer(help="Guided setup: wizard (default), systemd services, rclone, fail2ban, tailscale.")

app.add_typer(config_app, name="config")
app.add_typer(backup_app, name="backup")
app.add_typer(restore_app, name="restore")
app.add_typer(update_app, name="update")
app.add_typer(security_app, name="security")
app.add_typer(tailscale_app, name="tailscale")
app.add_typer(setup_app, name="setup")


def _load(config_path: Optional[str]) -> CompanionConfig:
    latest = version_check.check_for_update()
    if latest:
        console.print(
            f"[yellow]A new runtipi-companion version is available: {latest} "
            f"(installed: {__version__}). Run 'pip install --upgrade runtipi-companion'.[/yellow]"
        )
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
        raise typer.Exit(1) from e


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
    path: Optional[str] = typer.Option(
        None, "--path", help="Where to write the config file (default: asked interactively)."
    ),
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
    apps: Optional[str] = typer.Option(
        None, "--apps", help="Comma-separated app ids (default: all configured/installed)."
    ),
    remotes: Optional[str] = typer.Option(
        None, "--remote", help="Comma-separated remote names to sync to (default: all enabled)."
    ),
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
            notify(
                cfg.notify, f"runtipi-companion: {schedule} backup completed ({len(created)} archives)", success=True
            )
    except Exception as e:
        if not dry_run:
            notify(cfg.notify, f"runtipi-companion: {schedule} backup FAILED: {e}", success=False)
        raise


HostOption = typer.Option(
    None, "--host", help="With --remote/--from-remote: which machine's subtree (default: this machine's host label)."
)


def _require_remote_for_host(host: Optional[str], remote: Optional[str]) -> None:
    # Host subfolders only exist on remotes; local disk is one machine's.
    if host and not remote:
        console.print("[red]--host only applies to remote listings/restores (add --remote/--from-remote).[/red]")
        raise typer.Exit(1)


@backup_app.command("list")
def backup_list(
    app: Optional[str] = typer.Argument(None, help="App id. Omit to show every app's latest backup."),
    remote: Optional[str] = typer.Option(None, help="List backups on this remote instead of locally."),
    host: Optional[str] = HostOption,
    config: Optional[str] = ConfigOption,
):
    """List backups. Without APP: one line per app with its newest archive
    (also handy to look up app ids). With APP: every archive for that app."""
    _require_remote_for_host(host, remote)
    cfg = _load(config)

    if app is None:
        if remote:
            files = restore_mod._remote_files(cfg, remote, host or cfg.host_label)
        else:
            root = Path(cfg.backup_local_path)
            files = [str(p.relative_to(root)) for p in root.glob("*/*/*.tar.gz")]
        latest = restore_mod.latest_per_app(files)
        if not latest:
            console.print("No backups found.")
            _print_known_hosts(cfg, remote)
            return
        for store, app_id, newest in latest:
            console.print(f"{app_id}  [dim](store: {store}, latest: {newest})[/dim]")
        return

    if remote:
        files = restore_mod.list_remote_backups(cfg, remote, app, host=host)
    else:
        files = [str(p) for p in restore_mod.list_local_backups(cfg, app)]
    if not files:
        console.print("No backups found.")
        _print_known_hosts(cfg, remote)
    for f in files:
        console.print(f)


def _print_known_hosts(cfg: CompanionConfig, remote: Optional[str]) -> None:
    """After an empty remote listing, show which host subfolders DO exist --
    the archive you're looking for may live under another machine's label."""
    if not remote:
        return
    hosts = restore_mod.list_remote_hosts(cfg, remote)
    others = [h for h in hosts if h != cfg.host_label]
    if others:
        console.print(f"Other hosts with backups here: {', '.join(others)} (use --host)")


@backup_app.command("remotes")
def backup_remotes(config: Optional[str] = ConfigOption):
    """Interactively add/edit/remove/toggle the rclone backup remotes in
    your config file."""
    if not config_wizard.manage_remotes(config):
        raise typer.Exit(1)


# ---- restore ----


@restore_app.command("run")
def restore_run(
    app_id: Optional[str] = typer.Argument(None, help="App id. Omit (with no backup file) to pick interactively."),
    backup_file: Optional[str] = typer.Argument(None, help="Filename as shown by 'backup list' (or 'restore list')."),
    store: str = typer.Option("migrated", "--store", help="App store name (see 'runtipi-cli installed')."),
    from_remote: Optional[str] = typer.Option(None, "--from-remote", help="Download from this remote first."),
    host: Optional[str] = HostOption,
    config: Optional[str] = ConfigOption,
    yes: bool = YesOption,
    dry_run: bool = DryRunOption,
):
    """Restore an app. --from-remote with --host restores another machine's
    remote backup onto this one (migration path); the interactive picker
    offers every host it finds on the chosen remote."""
    _require_remote_for_host(host, from_remote)
    cfg = _load(config)
    if app_id is None or backup_file is None:
        if not sys.stdin.isatty():
            console.print("[red]APP_ID and BACKUP_FILE are required when not running interactively.[/red]")
            raise typer.Exit(1)
        selection = tui.interactive_restore(cfg)
        if selection is None:
            raise typer.Exit(1)
        app_id = selection.app_id
        backup_file = selection.backup_file
        store = selection.store
        from_remote = selection.from_remote
        host = selection.host
    restore_mod.restore_backup(
        cfg, store, app_id, backup_file, from_remote=from_remote, host=host, assume_yes=yes, dry_run=dry_run
    )


@restore_app.command("list")
def restore_list(
    app_id: Optional[str] = typer.Argument(None),
    remote: Optional[str] = typer.Option(None),
    host: Optional[str] = HostOption,
    config: Optional[str] = ConfigOption,
):
    backup_list(app_id, remote, host, config)


# ---- update ----


BackupFirstOption = typer.Option(
    None, "--backup/--no-backup", help="Snapshot affected apps first (default: updates.backup_before)."
)


@update_app.command("apps")
def update_apps_cmd(
    apps: Optional[str] = typer.Option(None, "--apps", help="Comma-separated app ids (default: all)."),
    backup: Optional[bool] = BackupFirstOption,
    config: Optional[str] = ConfigOption,
    dry_run: bool = DryRunOption,
):
    cfg = _load(config)
    app_list = apps.split(",") if apps else None
    update_mod.update_apps(cfg, apps=app_list, dry_run=dry_run, backup_first=backup)


@update_app.command("core")
def update_core_cmd(
    version: str = typer.Option("latest"),
    backup: Optional[bool] = BackupFirstOption,
    config: Optional[str] = ConfigOption,
    dry_run: bool = DryRunOption,
):
    cfg = _load(config)
    update_mod.update_core(cfg, version, dry_run=dry_run, backup_first=backup)


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
    tailscale_security: bool = typer.Option(
        False, "--tailscale-security", help="VPN-only lockdown: tailscale ssh + ufw allow-tailscale0-only."
    ),
    all_: bool = typer.Option(False, "--all"),
    interactive: bool = typer.Option(False, "--interactive", "-i", help="Pick what to harden from a menu."),
    force: bool = typer.Option(False, "--force", help="Skip the authorized_keys safety check for SSH hardening."),
    config: Optional[str] = ConfigOption,
    yes: bool = YesOption,
    dry_run: bool = DryRunOption,
):
    """Apply the runtipi VPS-security / server-hardening checklist. Defaults
    to a dry-run preview -- pass --apply to actually change sshd_config,
    firewall rules, install fail2ban, or lock down to tailscale-only access."""
    cfg = _load(config)
    selected = {"ssh": ssh, "ufw": ufw, "fail2ban": fail2ban, "tailscale_security": tailscale_security}

    if all_:
        selected = {k: True for k in selected}
    elif interactive or (not any(selected.values()) and sys.stdin.isatty()):
        selected = tui.select_hardening()
    elif not any(selected.values()):
        console.print("Nothing selected. Pass --ssh, --ufw, --fail2ban, --tailscale-security, --all, or --interactive.")
        raise typer.Exit(1)

    if not any(selected.values()):
        console.print("Nothing selected.")
        raise typer.Exit(0)

    if selected["ssh"]:
        security_mod.harden_ssh(cfg, dry_run=dry_run, assume_yes=yes, force=force)
    if selected["ufw"]:
        security_mod.harden_ufw(cfg, dry_run=dry_run, assume_yes=yes)
    if selected["fail2ban"]:
        security_mod.harden_fail2ban(cfg, dry_run=dry_run, assume_yes=yes)
    if selected["tailscale_security"]:
        security_mod.harden_tailscale_security(cfg, dry_run=dry_run, assume_yes=yes)


@security_app.command("status")
def security_status(config: Optional[str] = ConfigOption):
    cfg = _load(config)
    security_mod.status(cfg)


# ---- tailscale ----


@tailscale_app.command("status")
def tailscale_status():
    tailscale_mod.status()


# ---- setup ----


@setup_app.callback(invoke_without_command=True)
def setup_default(
    ctx: typer.Context,
    config: Optional[str] = ConfigOption,
    yes: bool = YesOption,
    dry_run: bool = DryRunOption,
):
    """With no subcommand, runs the setup wizard."""
    if ctx.invoked_subcommand is not None:
        return
    cfg = _load(config)
    setup_wizard.run_wizard(cfg, dry_run=dry_run, assume_yes=yes)


@setup_app.command("wizard")
def setup_wizard_cmd(config: Optional[str] = ConfigOption, yes: bool = YesOption, dry_run: bool = DryRunOption):
    """Guided first-run: clone/verify the runtipi install, locate
    runtipi-cli, prepare/start, create backup dirs, sanity-check rclone."""
    cfg = _load(config)
    setup_wizard.run_wizard(cfg, dry_run=dry_run, assume_yes=yes)


@setup_app.command("services")
def setup_services_cmd(
    schedules: str = typer.Option(
        ",".join(services_mod.DEFAULT_SCHEDULES),
        "--schedules",
        help="Comma-separated: daily,weekly,monthly,yearly.",
    ),
    yes: bool = YesOption,
    dry_run: bool = DryRunOption,
):
    """Install and enable the bundled systemd backup timers (the packaged
    alternative to setting up cron by hand)."""
    try:
        services_mod.install_services(
            [s.strip() for s in schedules.split(",") if s.strip()], dry_run=dry_run, assume_yes=yes
        )
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e


@setup_app.command("rclone")
def setup_rclone_cmd(config: Optional[str] = ConfigOption, yes: bool = YesOption, dry_run: bool = DryRunOption):
    """Install rclone and walk through configuring the remotes your config
    references ('rclone config' itself stays interactive)."""
    cfg = _load(config)
    rclone_setup.setup_rclone(cfg, dry_run=dry_run, assume_yes=yes)


@setup_app.command("fail2ban")
def setup_fail2ban_cmd(config: Optional[str] = ConfigOption, yes: bool = YesOption, dry_run: bool = DryRunOption):
    """Install and configure fail2ban for sshd (same as
    'security harden --fail2ban')."""
    cfg = _load(config)
    security_mod.harden_fail2ban(cfg, dry_run=dry_run, assume_yes=yes)


@setup_app.command("tailscale")
def setup_tailscale_cmd(config: Optional[str] = ConfigOption, yes: bool = YesOption, dry_run: bool = DryRunOption):
    """Install tailscale and bring it up (auth key read from the env var
    named by tailscale.auth_key_env)."""
    cfg = _load(config)
    tailscale_mod.install_tailscale(cfg, dry_run=dry_run, assume_yes=yes)


@app.command("doctor")
def doctor_cmd(config: Optional[str] = ConfigOption):
    """One-shot health audit: runtipi install, docker, backups, rclone
    remotes, and the VPS-security checklist. Reports pass/warn/fail without
    changing anything; exits non-zero if any check fails."""
    cfg = _load(config)
    failed = doctor_mod.render(doctor_mod.run_doctor(cfg))
    raise typer.Exit(1 if failed else 0)


@app.command("self-update")
def self_update_cmd(dry_run: bool = DryRunOption):
    """Update runtipi-companion itself to the latest PyPI release (detects
    pipx vs pip installs). Defaults to a dry-run preview like everything
    else; pass --apply to actually upgrade."""
    if __version__ == "0.0.0+unknown":
        console.print("Source checkout -- not managed by pip/pipx. Use git pull instead.")
        raise typer.Exit(1)
    latest = version_check.check_for_update(force=True)
    if latest is None:
        console.print(f"[green]runtipi-companion {__version__} is up to date.[/green]")
        return
    # pipx installs run from a venv under a "pipx" directory; plain pip
    # venvs need pip invoked inside the same interpreter.
    if "pipx" in sys.prefix:
        cmd = ["pipx", "upgrade", "runtipi-companion"]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "runtipi-companion"]
    console.print(f"Upgrading {__version__} -> {latest}")
    shell_run(cmd, dry_run=dry_run)
    if not dry_run:
        console.print(f"[green]Upgraded to {latest}.[/green]")


@app.command("version")
def version_cmd():
    """Print the installed version and check PyPI for a newer release."""
    console.print(f"runtipi-companion {__version__}")
    latest = version_check.check_for_update(force=True)
    if latest:
        console.print(f"[yellow]A newer version is available: {latest}[/yellow]")
    else:
        console.print("[green]Up to date.[/green]")


if __name__ == "__main__":
    app()
