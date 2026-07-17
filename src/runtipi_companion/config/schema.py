from __future__ import annotations

import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

DEFAULT_CONFIG_PATHS = [
    Path("/etc/runtipi-companion/config.yaml"),
    Path.home() / ".config" / "runtipi-companion" / "config.yaml",
]

# Schema version written into config files as `version:`. Files without the
# key are treated as version 1 (pre-versioning). Bump this together with a
# new migration step in migrations.py whenever the config shape changes.
CONFIG_VERSION = 2

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
    # Subfolder on every REMOTE that this machine's backups sync into:
    # <remote>/<host_label>/<store>/<app>/. Local disk stays flat
    # (<local_path>/<store>/<app>/). Defaults to the machine's hostname, so
    # several boxes can share one bucket without clobbering or pruning each
    # other.
    host_label: Optional[str] = None
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
    # Apprise URLs (https://github.com/caronc/apprise): ntfy://, discord://,
    # mailto://, ... Every URL gets every notification.
    urls: list = field(default_factory=list)
    # Deprecated: single generic JSON webhook, kept for backward compatibility.
    webhook_url: Optional[str] = None
    notify_on_success: bool = False
    notify_on_failure: bool = True


@dataclass
class CompanionConfig:
    version: int = CONFIG_VERSION
    runtipi: RuntipiConfig = field(default_factory=RuntipiConfig)
    backup: BackupConfig = field(default_factory=BackupConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    tailscale: TailscaleConfig = field(default_factory=TailscaleConfig)
    updates: UpdatesConfig = field(default_factory=UpdatesConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)

    @property
    def backup_local_path(self) -> str:
        return self.backup.local_path or str(Path(self.runtipi.path) / "backups")

    @property
    def host_label(self) -> str:
        return self.backup.host_label or socket.gethostname()
