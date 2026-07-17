from __future__ import annotations

import json
import urllib.request

from ..config import NotifyConfig


def notify(cfg: NotifyConfig, message: str, *, success: bool = True) -> None:
    """Best-effort webhook notification (Discord/Slack/ntfy-compatible JSON
    body). Failures here are swallowed on purpose -- a broken webhook
    shouldn't fail a backup/update run that otherwise succeeded.
    """
    if not cfg.webhook_url:
        return
    if success and not cfg.notify_on_success:
        return
    if not success and not cfg.notify_on_failure:
        return
    payload = json.dumps({"text": message, "content": message}).encode()
    req = urllib.request.Request(
        cfg.webhook_url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass
