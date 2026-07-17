"""Load and validate the YAML config file into the dataclasses from schema.py."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

from .schema import (
    DEFAULT_CONFIG_PATHS,
    VALID_SCHEDULES,
    BackupConfig,
    CompanionConfig,
    ConfigError,
    Fail2BanConfig,
    NotifyConfig,
    RemoteConfig,
    RuntipiConfig,
    ScheduleConfig,
    SecurityConfig,
    SSHConfig,
    TailscaleConfig,
    TailscaleOnlyConfig,
    UFWConfig,
    UpdatesConfig,
)


def _schedules_from_dict(raw: Optional[dict]) -> dict:
    out = {}
    for name, val in (raw or {}).items():
        if name not in VALID_SCHEDULES:
            raise ConfigError(f"Unknown schedule '{name}', expected one of {VALID_SCHEDULES}")
        retention = val.get("retention", 3) if isinstance(val, dict) else int(val)
        out[name] = ScheduleConfig(retention=retention)
    return out


def _remotes_from_list(raw: Optional[list]) -> list:
    remotes = []
    seen = set()
    for item in raw or []:
        name = item.get("name")
        if not name:
            raise ConfigError("Each backup remote requires a 'name'")
        if name in seen:
            raise ConfigError(f"Duplicate remote name '{name}'")
        seen.add(name)
        rclone_remote = item.get("rclone_remote")
        if not rclone_remote:
            raise ConfigError(f"Remote '{name}' is missing 'rclone_remote'")
        remotes.append(
            RemoteConfig(
                name=name,
                rclone_remote=rclone_remote,
                enabled=item.get("enabled", True),
                schedules=_schedules_from_dict(item.get("schedules", {})),
                bandwidth_limit=item.get("bandwidth_limit"),
                extra_rclone_flags=item.get("extra_rclone_flags", []),
            )
        )
    return remotes


def load_config(path: Optional[str] = None) -> CompanionConfig:
    candidates = [Path(path)] if path else DEFAULT_CONFIG_PATHS
    chosen = next((p for p in candidates if p.exists()), None)
    if chosen is None:
        searched = ", ".join(str(p) for p in candidates)
        raise ConfigError(
            f"No config file found. Searched: {searched}\n" f"Run 'runtipi-companion config init' to create one."
        )
    with open(chosen) as f:
        raw = yaml.safe_load(f) or {}

    cfg = CompanionConfig()

    if "runtipi" in raw:
        r = raw["runtipi"] or {}
        cfg.runtipi = RuntipiConfig(
            path=r.get("path", cfg.runtipi.path),
            cli_path=r.get("cli_path"),
            apps=r.get("apps", []),
        )

    if "backup" in raw:
        b = raw["backup"] or {}
        schedules = _schedules_from_dict(b.get("schedules")) or cfg.backup.schedules
        cfg.backup = BackupConfig(
            work_dir=b.get("work_dir", cfg.backup.work_dir),
            local_path=b.get("local_path"),
            stop_apps=b.get("stop_apps", True),
            sleep_duration=b.get("sleep_duration", 10),
            schedules=schedules,
            remotes=_remotes_from_list(b.get("remotes", [])),
        )

    if "security" in raw:
        s = raw["security"] or {}
        ssh = s.get("ssh", {}) or {}
        ufw = s.get("ufw", {}) or {}
        f2b = s.get("fail2ban", {}) or {}
        ts_only = s.get("tailscale_only", {}) or {}
        cfg.security = SecurityConfig(
            ssh=SSHConfig(
                disable_password_auth=ssh.get("disable_password_auth", True),
                disable_root_login=ssh.get("disable_root_login", True),
                port=ssh.get("port"),
            ),
            ufw=UFWConfig(
                allowed_tcp_ports=ufw.get("allowed_tcp_ports", [22]),
                enable=ufw.get("enable", True),
            ),
            fail2ban=Fail2BanConfig(
                enabled=f2b.get("enabled", True),
                maxretry=f2b.get("maxretry", 3),
                bantime=f2b.get("bantime", 3600),
            ),
            tailscale_only=TailscaleOnlyConfig(
                enabled=ts_only.get("enabled", False),
                tailscale_ssh=ts_only.get("tailscale_ssh", True),
                tailscale_port_udp=ts_only.get("tailscale_port_udp", 41641),
            ),
        )

    if "tailscale" in raw:
        t = raw["tailscale"] or {}
        cfg.tailscale = TailscaleConfig(
            enabled=t.get("enabled", False),
            auth_key_env=t.get("auth_key_env", "TAILSCALE_AUTHKEY"),
            advertise_exit_node=t.get("advertise_exit_node", False),
            ssh=t.get("ssh", False),
        )

    if "updates" in raw:
        u = raw["updates"] or {}
        cfg.updates = UpdatesConfig(
            auto_update_core=u.get("auto_update_core", False),
            auto_update_apps=u.get("auto_update_apps", False),
            exclude_apps=u.get("exclude_apps", []),
            backup_before=u.get("backup_before", True),
        )

    if "notify" in raw:
        n = raw["notify"] or {}
        cfg.notify = NotifyConfig(
            urls=n.get("urls", []),
            webhook_url=n.get("webhook_url"),
            notify_on_success=n.get("notify_on_success", False),
            notify_on_failure=n.get("notify_on_failure", True),
        )

    validate_config(cfg)
    return cfg


def validate_config(cfg: CompanionConfig) -> None:
    if not Path(cfg.runtipi.path).is_absolute():
        raise ConfigError("runtipi.path must be an absolute path")
    for remote in cfg.backup.remotes:
        if not remote.schedules:
            raise ConfigError(
                f"Remote '{remote.name}' has no schedules/retention configured. "
                f"Add at least one of {VALID_SCHEDULES} under its 'schedules' key."
            )
