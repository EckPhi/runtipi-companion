from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

DEFAULT_CONFIG_PATHS = [
    Path("/etc/runtipi-companion/config.yaml"),
    Path.home() / ".config" / "runtipi-companion" / "config.yaml",
]

VALID_SCHEDULES = ("daily", "weekly", "monthly", "yearly")


class ConfigError(RuntimeError):
    pass


@dataclass
class ScheduleConfig:
    retention: int = 3


@dataclass
class RemoteConfig:
    name: str
    rclone_remote: str  # e.g. "b2-runtipi:my-bucket/runtipi-backups"
    enabled: bool = True
    schedules: dict = field(default_factory=dict)  # str -> ScheduleConfig
    bandwidth_limit: Optional[str] = None  # rclone --bwlimit value, e.g. "5M"
    extra_rclone_flags: list = field(default_factory=list)

    def retention_for(self, schedule: str) -> Optional[int]:
        sched = self.schedules.get(schedule)
        return sched.retention if sched else None


@dataclass
class RuntipiConfig:
    path: str = "/opt/runtipi"
    cli_path: Optional[str] = None  # explicit path to runtipi-cli; auto-detected if None
    apps: list = field(default_factory=list)  # empty = all installed apps


@dataclass
class BackupConfig:
    work_dir: str = "/tmp/runtipi-companion"
    local_path: Optional[str] = None  # defaults to <runtipi.path>/backups
    stop_apps: bool = True
    sleep_duration: int = 10
    schedules: dict = field(
        default_factory=lambda: {
            "daily": ScheduleConfig(retention=3),
            "weekly": ScheduleConfig(retention=3),
            "monthly": ScheduleConfig(retention=3),
            "yearly": ScheduleConfig(retention=3),
        }
    )
    remotes: list = field(default_factory=list)  # list[RemoteConfig]

    def remote(self, name: str) -> Optional[RemoteConfig]:
        for r in self.remotes:
            if r.name == name:
                return r
        return None


@dataclass
class SSHConfig:
    disable_password_auth: bool = True
    disable_root_login: bool = True
    port: Optional[int] = None  # None = leave untouched


@dataclass
class UFWConfig:
    allowed_tcp_ports: list = field(default_factory=lambda: [22])
    enable: bool = True


@dataclass
class Fail2BanConfig:
    enabled: bool = True
    maxretry: int = 3
    bantime: int = 3600


@dataclass
class TailscaleOnlyConfig:
    """VPN-only lockdown: reachable only over the tailscale0 interface.
    See https://tailscale.com/kb/1077/secure-server-ufw and the runtipi VPS
    security guide's "Option A: VPN access only".
    """

    enabled: bool = False
    tailscale_ssh: bool = True  # `tailscale up --ssh`, replaces public sshd exposure
    tailscale_port_udp: int = 41641  # tailscale's own coordination port; must stay reachable publicly


@dataclass
class SecurityConfig:
    ssh: SSHConfig = field(default_factory=SSHConfig)
    ufw: UFWConfig = field(default_factory=UFWConfig)
    fail2ban: Fail2BanConfig = field(default_factory=Fail2BanConfig)
    tailscale_only: TailscaleOnlyConfig = field(default_factory=TailscaleOnlyConfig)


@dataclass
class TailscaleConfig:
    enabled: bool = False
    auth_key_env: str = "TAILSCALE_AUTHKEY"
    advertise_exit_node: bool = False
    ssh: bool = False  # tailscale ssh


@dataclass
class UpdatesConfig:
    auto_update_core: bool = False
    auto_update_apps: bool = False
    exclude_apps: list = field(default_factory=list)
    backup_before: bool = True  # local pre-update snapshot before update apps/core


@dataclass
class NotifyConfig:
    webhook_url: Optional[str] = None
    notify_on_success: bool = False
    notify_on_failure: bool = True


@dataclass
class CompanionConfig:
    runtipi: RuntipiConfig = field(default_factory=RuntipiConfig)
    backup: BackupConfig = field(default_factory=BackupConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    tailscale: TailscaleConfig = field(default_factory=TailscaleConfig)
    updates: UpdatesConfig = field(default_factory=UpdatesConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)

    @property
    def backup_local_path(self) -> str:
        return self.backup.local_path or str(Path(self.runtipi.path) / "backups")


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
            f"No config file found. Searched: {searched}\n"
            f"Run 'runtipi-companion config init' to create one."
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
