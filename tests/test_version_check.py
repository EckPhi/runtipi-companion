import json

import runtipi_companion.system.version_check as vc


def test_parse_version_plain():
    assert vc._parse_version("1.2.3") == (1, 2, 3)


def test_parse_version_dev_suffix():
    assert vc._parse_version("1.2.3.dev4") == (1, 2, 3)


def test_parse_version_unparsable_tail():
    assert vc._parse_version("1.2.rc1") == (1, 2)


def test_check_for_update_skips_source_checkout(monkeypatch, tmp_path):
    monkeypatch.setattr(vc, "__version__", "0.0.0+unknown")
    monkeypatch.setattr(vc, "CACHE_PATH", tmp_path / "cache.json")
    assert vc.check_for_update() is None


def test_check_for_update_reports_newer(monkeypatch, tmp_path):
    monkeypatch.setattr(vc, "__version__", "1.0.0")
    monkeypatch.setattr(vc, "CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(vc, "_fetch_latest", lambda timeout=2.0: "1.1.0")
    assert vc.check_for_update() == "1.1.0"


def test_check_for_update_none_when_current(monkeypatch, tmp_path):
    monkeypatch.setattr(vc, "__version__", "1.1.0")
    monkeypatch.setattr(vc, "CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(vc, "_fetch_latest", lambda timeout=2.0: "1.1.0")
    assert vc.check_for_update() is None


def test_check_for_update_uses_cache_within_interval(monkeypatch, tmp_path):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps({"checked_at": __import__("time").time(), "latest": "9.9.9"}))
    monkeypatch.setattr(vc, "__version__", "1.0.0")
    monkeypatch.setattr(vc, "CACHE_PATH", cache_path)

    def _boom(timeout=2.0):
        raise AssertionError("should not hit the network when cache is fresh")

    monkeypatch.setattr(vc, "_fetch_latest", _boom)
    assert vc.check_for_update() == "9.9.9"


def test_check_for_update_none_on_network_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(vc, "__version__", "1.0.0")
    monkeypatch.setattr(vc, "CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(vc, "_fetch_latest", lambda timeout=2.0: None)
    assert vc.check_for_update() is None
