"""Pure field validators shared by the form wizard.

Each returns None when the value is acceptable, or a human-readable error
string. Kept free of textual imports so they unit-test as plain functions;
form_wizard wraps them into textual Validator objects for realtime checking.
"""

from __future__ import annotations

from typing import Optional


def absolute_path(value: str) -> Optional[str]:
    value = value.strip()
    if not value:
        return "Required."
    if not value.startswith("/"):
        return "Must be an absolute path (start with /)."
    return None


def optional_absolute_path(value: str) -> Optional[str]:
    if not value.strip():
        return None
    return absolute_path(value)


def required_int(value: str, minimum: int = 1, maximum: int = 9999) -> Optional[str]:
    value = value.strip()
    if not value.isdigit():
        return "Enter a whole number."
    if not minimum <= int(value) <= maximum:
        return f"Must be between {minimum} and {maximum}."
    return None


def optional_port(value: str) -> Optional[str]:
    if not value.strip():
        return None
    return required_int(value, 1, 65535)


def csv_ports(value: str) -> Optional[str]:
    parts = [p.strip() for p in value.split(",") if p.strip()]
    if not parts:
        return "At least one port (e.g. 22)."
    for part in parts:
        if err := required_int(part, 1, 65535):
            return f"'{part}': {err}"
    return None


def rclone_target(value: str) -> Optional[str]:
    value = value.strip()
    if not value:
        return "Required."
    remote, sep, _ = value.partition(":")
    if not sep or not remote:
        return "Format: <rclone-remote>:<path>, e.g. b2-runtipi:my-bucket/backups."
    if value.endswith("/"):
        return "No trailing slash."
    return None


def remote_name(value: str) -> Optional[str]:
    value = value.strip()
    if not value:
        return "Required."
    if "/" in value or ":" in value or " " in value:
        return "Short name without /, : or spaces."
    return None


def apprise_url(value: str) -> Optional[str]:
    """Realtime check of a single apprise URL via apprise itself."""
    value = value.strip()
    if not value:
        return "Required (remove the row if unused)."
    import apprise  # lazy: costs a few hundred ms on first use

    if not apprise.Apprise().add(value):
        return "Not a valid apprise URL."
    return None
