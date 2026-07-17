"""Configuration package: dataclasses (schema), YAML loading (loader), and
the bundled example config (templates). Everything public is re-exported
here so callers just use `from ..config import ...`."""

from .loader import load_config, validate_config
from .migrations import migrate, migrate_file
from .schema import (
    CONFIG_VERSION,
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

__all__ = [
    "CONFIG_VERSION",
    "DEFAULT_CONFIG_PATHS",
    "VALID_SCHEDULES",
    "BackupConfig",
    "CompanionConfig",
    "ConfigError",
    "Fail2BanConfig",
    "NotifyConfig",
    "RemoteConfig",
    "RuntipiConfig",
    "ScheduleConfig",
    "SecurityConfig",
    "SSHConfig",
    "TailscaleConfig",
    "TailscaleOnlyConfig",
    "UFWConfig",
    "UpdatesConfig",
    "load_config",
    "migrate",
    "migrate_file",
    "validate_config",
]
