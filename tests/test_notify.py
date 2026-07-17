import sys
import types

from runtipi_companion.config import NotifyConfig
from runtipi_companion.system import notify as notify_mod


class StubAppriseClient:
    instances = []

    def __init__(self):
        self.added = []
        self.notifications = []
        StubAppriseClient.instances.append(self)

    def add(self, url):
        self.added.append(url)
        return not url.startswith("bad://")

    def notify(self, *, title, body, notify_type):
        self.notifications.append((title, body, notify_type))
        return True


def _stub_apprise(monkeypatch):
    StubAppriseClient.instances = []
    stub = types.SimpleNamespace(
        Apprise=StubAppriseClient,
        NotifyType=types.SimpleNamespace(SUCCESS="success", FAILURE="failure"),
    )
    monkeypatch.setitem(sys.modules, "apprise", stub)
    return stub


def test_notify_sends_to_all_apprise_urls(monkeypatch):
    _stub_apprise(monkeypatch)
    cfg = NotifyConfig(urls=["ntfy://host/topic", "discord://id/token"], notify_on_success=True)
    notify_mod.notify(cfg, "backup done", success=True)
    (client,) = StubAppriseClient.instances
    assert client.added == ["ntfy://host/topic", "discord://id/token"]
    assert client.notifications == [("runtipi-companion", "backup done", "success")]


def test_notify_failure_uses_failure_type(monkeypatch):
    _stub_apprise(monkeypatch)
    cfg = NotifyConfig(urls=["ntfy://host/topic"])
    notify_mod.notify(cfg, "backup FAILED", success=False)
    (client,) = StubAppriseClient.instances
    assert client.notifications == [("runtipi-companion", "backup FAILED", "failure")]


def test_notify_success_suppressed_by_default(monkeypatch):
    _stub_apprise(monkeypatch)
    cfg = NotifyConfig(urls=["ntfy://host/topic"])  # notify_on_success defaults False
    notify_mod.notify(cfg, "backup done", success=True)
    assert StubAppriseClient.instances == []


def test_notify_invalid_url_skipped_but_rest_sent(monkeypatch):
    _stub_apprise(monkeypatch)
    cfg = NotifyConfig(urls=["bad://nope", "ntfy://host/topic"], notify_on_success=True)
    notify_mod.notify(cfg, "hi", success=True)
    (client,) = StubAppriseClient.instances
    assert client.notifications  # still notified via the valid URL


def test_legacy_webhook_still_posts(monkeypatch):
    sent = []
    monkeypatch.setattr(notify_mod, "_notify_legacy_webhook", lambda url, msg: sent.append((url, msg)))
    cfg = NotifyConfig(webhook_url="https://hooks.example/x", notify_on_success=True)
    notify_mod.notify(cfg, "backup done", success=True)
    assert sent == [("https://hooks.example/x", "backup done")]


def test_no_channels_configured_is_a_noop(monkeypatch):
    _stub_apprise(monkeypatch)
    notify_mod.notify(NotifyConfig(notify_on_success=True), "hi", success=True)
    assert StubAppriseClient.instances == []
