"""Backup application configuration."""

from .loader import load_config, validate_config
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
    "validate_config",
]
