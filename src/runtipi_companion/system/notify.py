"""Notifications via apprise (ntfy, Discord, Slack, email, matrix, and ~80
other services -- see https://github.com/caronc/apprise). The legacy single
`webhook_url` (plain JSON POST) keeps working but is deprecated in favor of
`notify.urls`.
"""

from __future__ import annotations

import json
import urllib.request

from rich.console import Console

from ..config import NotifyConfig

console = Console()


def notify(cfg: NotifyConfig, message: str, *, success: bool = True, title: str = "runtipi-companion") -> None:
    """Best-effort notification to every configured channel. Failures here
    are swallowed on purpose -- a broken notification channel shouldn't fail
    a backup/update run that otherwise succeeded.
    """
    if success and not cfg.notify_on_success:
        return
    if not success and not cfg.notify_on_failure:
        return
    if cfg.urls:
        _notify_apprise(cfg.urls, title, message, success)
    if cfg.webhook_url:
        _notify_legacy_webhook(cfg.webhook_url, message)


def _notify_apprise(urls: list, title: str, body: str, success: bool) -> None:
    try:
        # Imported lazily: apprise costs a few hundred ms to import, which
        # every CLI invocation would otherwise pay even with no URLs set.
        import apprise

        client = apprise.Apprise()
        for url in urls:
            if not client.add(url):
                console.print(f"[yellow]Invalid apprise URL skipped: {url}[/yellow]")
        client.notify(
            title=title,
            body=body,
            notify_type=apprise.NotifyType.SUCCESS if success else apprise.NotifyType.FAILURE,
        )
    except Exception:
        pass


def _notify_legacy_webhook(url: str, message: str) -> None:
    payload = json.dumps({"text": message, "content": message}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass
