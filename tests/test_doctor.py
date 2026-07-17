import time

from runtipi_companion.config import CompanionConfig
from runtipi_companion.doctor import FAIL, OK, evaluate_sshd_config, newest_backup_age

SSHD_HARDENED = "passwordauthentication no\npermitrootlogin no\nport 22\n"
SSHD_DEFAULT = "passwordauthentication yes\npermitrootlogin yes\nport 22\n"


def test_evaluate_sshd_config_hardened_passes():
    results = evaluate_sshd_config(SSHD_HARDENED, CompanionConfig())
    assert [r.status for r in results] == [OK, OK]


def test_evaluate_sshd_config_default_fails():
    results = evaluate_sshd_config(SSHD_DEFAULT, CompanionConfig())
    assert [r.status for r in results] == [FAIL, FAIL]


def test_evaluate_sshd_config_prohibit_password_counts_as_disabled_root():
    output = "passwordauthentication no\npermitrootlogin prohibit-password\n"
    results = evaluate_sshd_config(output, CompanionConfig())
    assert [r.status for r in results] == [OK, OK]


def test_evaluate_sshd_config_custom_port():
    cfg = CompanionConfig()
    cfg.security.ssh.port = 2847
    results = evaluate_sshd_config("passwordauthentication no\npermitrootlogin no\nport 22\n", cfg)
    assert results[-1].status == FAIL
    results = evaluate_sshd_config("passwordauthentication no\npermitrootlogin no\nport 2847\n", cfg)
    assert results[-1].status == OK


def test_evaluate_sshd_config_skips_disabled_checks():
    cfg = CompanionConfig()
    cfg.security.ssh.disable_password_auth = False
    cfg.security.ssh.disable_root_login = False
    assert evaluate_sshd_config(SSHD_DEFAULT, cfg) == []


def test_newest_backup_age_empty_dir(tmp_path):
    assert newest_backup_age(tmp_path) is None


def test_newest_backup_age_finds_newest(tmp_path):
    old = tmp_path / "store" / "app" / "app-daily-2026-07-01.tar.gz"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"old")
    new = tmp_path / "store" / "app" / "app-daily-2026-07-17.tar.gz"
    new.write_bytes(b"new")
    now = time.time()
    import os

    os.utime(old, (now - 10 * 86400, now - 10 * 86400))
    os.utime(new, (now - 3600, now - 3600))
    age = newest_backup_age(tmp_path)
    assert age is not None
    assert 3000 < age < 4200
