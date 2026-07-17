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


def notify_test(cfg: NotifyConfig) -> bool:
    """Send a test message to every configured channel and report per-channel
    results -- the loud counterpart to notify(), which swallows failures.
    Ignores the notify_on_success/notify_on_failure gates (a test should
    always send). Returns True only if every channel delivered.
    """
    import socket

    if not cfg.urls and not cfg.webhook_url:
        console.print("[yellow]No notification channels configured (notify.urls / notify.webhook_url).[/yellow]")
        return False

    message = f"Test notification from runtipi-companion on {socket.gethostname()}"
    ok = True

    if cfg.urls:
        import apprise

        client = apprise.Apprise()
        for url in cfg.urls:
            if not client.add(url):
                console.print(f"[red]Invalid apprise URL: {url}[/red]")
                ok = False
        if len(client):
            if client.notify(title="runtipi-companion test", body=message, notify_type=apprise.NotifyType.INFO):
                console.print(f"[green]apprise: sent to {len(client)} channel(s).[/green]")
            else:
                console.print("[red]apprise: delivery failed (see log output above).[/red]")
                ok = False

    if cfg.webhook_url:
        try:
            payload = json.dumps({"text": message, "content": message}).encode()
            req = urllib.request.Request(cfg.webhook_url, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                console.print(f"[green]webhook: HTTP {resp.status}.[/green]")
        except Exception as e:
            console.print(f"[red]webhook: {e}[/red]")
            ok = False

    if not cfg.notify_on_success:
        console.print("[dim]Note: notify_on_success is off -- routine successful backups won't notify.[/dim]")
    return ok
