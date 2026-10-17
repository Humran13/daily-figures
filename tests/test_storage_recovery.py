"""Regression tests for the storage-recovery hardening work.

Covers: command help never depending on valid state (the exact bug behind
the production incident), the repair-installation recovery command,
migrate-storage's explicit --source/--preserve-existing-destination
workflow, upload-conflict handling, doctor --recovery diagnostics, and the
Compose files that keep a bare `docker compose up` from using the wrong
storage directory.
"""

from __future__ import annotations

import getpass
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.test_deployment import (
    MANAGER,
    ROOT,
    _bash,
    _bash_path,
    _write_install_state,
)

PUBLIC_COMMANDS = [
    "install",
    "update",
    "backup",
    "restore",
    "migrate-storage",
    "repair-installation",
    "post-restore",
    "configure-backups",
    "backup-check",
    "status",
    "start",
    "stop",
    "restart",
    "logs",
    "health",
    "doctor",
    "verify-db",
]


def _run(args: list[str], env: dict[str, str], timeout: int = 15) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_bash(), str(MANAGER), *args], env=env, capture_output=True, text=True, timeout=timeout
    )


def _minimal_env(tmp_path: Path) -> dict[str, str]:
    """Env with no installation state at all -- the harshest case for --help."""
    return os.environ | {
        "DF_TEST_MODE": "1",
        "DF_STATE_FILE": _bash_path(tmp_path / "missing-install.conf"),
        "DF_LOCK_FILE": _bash_path(tmp_path / "run" / "daily-figures.lock"),
        "DF_RUNTIME_DIR": _bash_path(tmp_path / "runtime"),
        "DF_SYSTEMD_DIR": _bash_path(tmp_path / "systemd"),
    }


# --- Section 1/14: --help must never depend on valid state --------------


@pytest.mark.parametrize("command", PUBLIC_COMMANDS)
def test_help_works_with_no_installation_state(tmp_path, command):
    env = _minimal_env(tmp_path)
    result = _run([command, "--help"], env)
    assert result.returncode == 0, result.stderr
    assert "Usage: daily-figures" in result.stdout
    assert not (tmp_path / "missing-install.conf").exists()


@pytest.mark.parametrize("command", PUBLIC_COMMANDS)
def test_short_help_flag_also_works(tmp_path, command):
    env = _minimal_env(tmp_path)
    result = _run([command, "-h"], env)
    assert result.returncode == 0, result.stderr
    assert "Usage: daily-figures" in result.stdout


def test_migrate_storage_help_with_unsafe_legacy_state_reproduces_incident_fix(tmp_path):
    """This is the exact bug from the incident: --help used to call
    load_state/check_env before parsing arguments, so an unsafe legacy state
    file made even --help fail with 'unsafe data path in state file'."""
    site_home = tmp_path / "home" / "fixture-user"
    paths = _write_install_state(
        tmp_path, data_dir=site_home / "htdocs" / "figures.example.com" / "data"
    )
    env = os.environ | {
        "DF_TEST_MODE": "1",
        "DF_STATE_FILE": _bash_path(paths["state"]),
    }
    result = _run(["migrate-storage", "--help"], env)
    assert result.returncode == 0, result.stderr
    assert "Usage: daily-figures migrate-storage" in result.stdout
    assert "unsafe data path" not in result.stderr


def test_repair_installation_help_has_zero_side_effects(tmp_path):
    env = _minimal_env(tmp_path)
    result = _run(["repair-installation", "--help"], env)
    assert result.returncode == 0, result.stderr
    assert "repair-installation" in result.stdout
    assert not (tmp_path / "missing-install.conf").exists()


def test_doctor_recovery_help_mentions_read_only_mode(tmp_path):
    env = _minimal_env(tmp_path)
    result = _run(["doctor", "--help"], env)
    assert result.returncode == 0, result.stderr
    assert "--recovery" in result.stdout
    assert "read-only" in result.stdout


def test_normal_commands_still_reject_unsafe_state_without_help(tmp_path):
    """Help is the exception; every other invocation must still fail safely
    and point at the recovery command."""
    site_home = tmp_path / "home" / "fixture-user"
    paths = _write_install_state(
        tmp_path, data_dir=site_home / "htdocs" / "figures.example.com" / "data"
    )
    env = os.environ | {"DF_TEST_MODE": "1", "DF_STATE_FILE": _bash_path(paths["state"])}
    result = _run(["status"], env)
    assert result.returncode != 0
    assert "repair-installation" in result.stderr


# --- Section 2/3/4: repair-installation -----------------------------------


def _real_sqlite_backup_tool(code_dir: Path) -> None:
    """Fixtures build a throwaway CODE_DIR; copy in the real backup tool so
    verify_database()/python_tool() can actually run against it."""
    scripts_dir = code_dir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "scripts" / "sqlite_backup.py", scripts_dir / "sqlite_backup.py")


def test_repair_installation_requires_existing_state(tmp_path):
    env = _minimal_env(tmp_path)
    result = _run(["repair-installation", "--yes"], env)
    assert result.returncode != 0
    assert "there is nothing to repair" in result.stderr


def test_repair_installation_rejects_unknown_option(tmp_path):
    paths = _write_install_state(tmp_path)
    env = os.environ | {"DF_TEST_MODE": "1", "DF_STATE_FILE": _bash_path(paths["state"])}
    result = _run(["repair-installation", "--bogus"], env)
    assert result.returncode != 0
    assert "unknown repair-installation option" in result.stderr


def test_parse_trusted_state_fields_never_trusts_legacy_storage_paths(tmp_path):
    """Legacy DATA_DIR/CONFIG_DIR/BACKUP_ROOT must be captured separately
    (R_OLD_*) and never conflated with the trusted scalar identity fields."""
    site_home = tmp_path / "home" / "fixture-user"
    legacy_data = site_home / "htdocs" / "figures.example.com" / "data"
    paths = _write_install_state(tmp_path, data_dir=legacy_data)
    env = os.environ | {"DF_TEST_MODE": "1", "DF_STATE_FILE": _bash_path(paths["state"])}
    result = _run(["_test-parse-trusted-state"], env)
    assert result.returncode == 0, result.stderr
    domain, site_user, code_dir, old_data, old_config, old_backup = result.stdout.strip().split("|")
    assert domain == "figures.example.com"
    assert site_user == "fixture-user"
    assert code_dir == _bash_path(paths["code_dir"])
    assert old_data == _bash_path(legacy_data)
    assert old_config == _bash_path(paths["config_dir"])
    assert old_backup == _bash_path(paths["backup_root"])


def test_repair_installation_full_recovery_generates_private_paths(tmp_path):
    """End-to-end: legacy state with storage under htdocs is repaired to the
    private app-data/app-config/app-backups layout, a timestamped backup of
    install.conf is created and never deleted, and the recovered state file
    is root-equivalent-owned, mode 0600."""
    site_user = getpass.getuser()
    domain = "figures.example.com"
    site_home = tmp_path / "home" / site_user
    legacy_dir = site_home / "htdocs" / domain / "data"
    site_dir = site_home / "htdocs" / domain
    code_dir = tmp_path / "opt" / "daily-figures"
    nginx = tmp_path / "nginx"
    for path in (legacy_dir, code_dir / ".git", nginx, site_dir):
        path.mkdir(parents=True, exist_ok=True)
    (nginx / f"{domain}.conf").write_text(
        f"server {{ server_name {domain}; proxy_pass http://127.0.0.1:5000; }}\n"
    )
    (legacy_dir / "production.db").write_bytes(b"legacy real production data")
    (legacy_dir / ".env").write_text("SECRET_KEY=legacy-secret-value\n", encoding="utf-8")
    (tmp_path / "systemd").mkdir(parents=True, exist_ok=True)

    state = tmp_path / "install.conf"
    values = {
        "DOMAIN": domain,
        "SITE_USER": site_user,
        "SITE_HOME": _bash_path(site_home),
        "SITE_DIR": _bash_path(site_dir),
        "VHOST_FILE": _bash_path(nginx / f"{domain}.conf"),
        "SITE_UID": "1001",
        "SITE_GID": "1001",
        "CODE_DIR": _bash_path(code_dir),
        "DATA_DIR": _bash_path(legacy_dir),
        "CONFIG_DIR": _bash_path(legacy_dir),
        "BACKUP_ROOT": _bash_path(legacy_dir / "backups"),
        "PORT": "5000",
        "COMPOSE_FILE": "docker-compose.production.yml",
        "BACKUP_RETENTION": "14",
        "BACKUP_ON_CALENDAR": "*-*-* 04:00:00",
    }
    state.write_text("".join(f"{key}='{value}'\n" for key, value in values.items()))

    env = os.environ | {
        "DF_TEST_MODE": "1",
        "DF_TEST_SITE_USER": site_user,
        "DF_STATE_FILE": _bash_path(state),
        "DF_HOME_ROOT": _bash_path(tmp_path / "home"),
        "DF_NGINX_DIRS": _bash_path(nginx),
        "DF_LOCK_FILE": _bash_path(tmp_path / "run" / "daily-figures.lock"),
        "DF_RUNTIME_DIR": _bash_path(tmp_path / "runtime"),
        "DF_SYSTEMD_DIR": _bash_path(tmp_path / "systemd"),
    }

    result = _run(["repair-installation", "--yes"], env, timeout=30)
    assert result.returncode == 0, result.stderr

    expected_data = site_home / "app-data" / "daily-figures"
    expected_config = site_home / "app-config" / "daily-figures"
    expected_backup = site_home / "app-backups" / "daily-figures"
    assert expected_data.is_dir()
    assert expected_config.is_dir()
    assert expected_backup.is_dir()

    repaired = state.read_text(encoding="utf-8")
    assert f"DATA_DIR={_bash_path(expected_data)}" in repaired.replace("'", "")
    assert f"CONFIG_DIR={_bash_path(expected_config)}" in repaired.replace("'", "")
    assert f"BACKUP_ROOT={_bash_path(expected_backup)}" in repaired.replace("'", "")
    assert "htdocs" not in _bash_path(expected_data)
    assert "htdocs" not in _bash_path(expected_config)
    assert "htdocs" not in _bash_path(expected_backup)

    backups = list(tmp_path.glob("install.conf.bak-*"))
    assert len(backups) == 1, "exactly one timestamped install.conf backup must exist"
    assert "htdocs" in backups[0].read_text(encoding="utf-8")  # the legacy file, preserved verbatim

    if os.name != "nt":
        # NTFS does not honor POSIX permission bits through Git Bash, so
        # this is only meaningful on a real POSIX filesystem (production).
        stat_result = subprocess.run(
            [_bash(), "-lc", f"stat -c '%a' '{_bash_path(state)}'"],
            capture_output=True, text=True, check=True,
        )
        assert stat_result.stdout.strip() == "600"

    recovered_env = (expected_config / ".env").read_text(encoding="utf-8")
    assert recovered_env == "SECRET_KEY=legacy-secret-value\n"
    assert "migrate-storage --source" in result.stdout


def test_repair_installation_without_yes_requires_confirmation_noninteractively(tmp_path):
    site_user = getpass.getuser()
    domain = "figures.example.com"
    site_home = tmp_path / "home" / site_user
    legacy_dir = site_home / "htdocs" / domain / "data"
    site_dir = site_home / "htdocs" / domain
    code_dir = tmp_path / "opt" / "daily-figures"
    nginx = tmp_path / "nginx"
    for path in (legacy_dir, code_dir / ".git", nginx, site_dir):
        path.mkdir(parents=True, exist_ok=True)
    (nginx / f"{domain}.conf").write_text(
        f"server {{ server_name {domain}; proxy_pass http://127.0.0.1:5000; }}\n"
    )
    state = tmp_path / "install.conf"
    values = {
        "DOMAIN": domain, "SITE_USER": site_user, "SITE_HOME": _bash_path(site_home),
        "SITE_DIR": _bash_path(site_dir), "VHOST_FILE": _bash_path(nginx / f"{domain}.conf"),
        "SITE_UID": "1001", "SITE_GID": "1001", "CODE_DIR": _bash_path(code_dir),
        "DATA_DIR": _bash_path(legacy_dir), "CONFIG_DIR": _bash_path(legacy_dir),
        "BACKUP_ROOT": _bash_path(legacy_dir / "backups"), "PORT": "5000",
        "COMPOSE_FILE": "docker-compose.production.yml", "BACKUP_RETENTION": "14",
        "BACKUP_ON_CALENDAR": "*-*-* 04:00:00",
    }
    state.write_text("".join(f"{key}='{value}'\n" for key, value in values.items()))
    env = os.environ | {
        "DF_TEST_MODE": "1", "DF_TEST_SITE_USER": site_user,
        "DF_STATE_FILE": _bash_path(state),
        "DF_HOME_ROOT": _bash_path(tmp_path / "home"),
        "DF_NGINX_DIRS": _bash_path(nginx),
        "DF_LOCK_FILE": _bash_path(tmp_path / "run" / "daily-figures.lock"),
        "DF_RUNTIME_DIR": _bash_path(tmp_path / "runtime"),
        "DF_SYSTEMD_DIR": _bash_path(tmp_path / "systemd"),
    }
    result = _run(["repair-installation"], env, timeout=30)
    assert result.returncode != 0
    assert "rerun with --yes" in result.stderr
    # Nothing was changed because confirmation was never given.
    assert state.read_text(encoding="utf-8") == state.read_text(encoding="utf-8")
    assert "DATA_DIR=" + _bash_path(legacy_dir) in state.read_text(encoding="utf-8").replace("'", "")


# --- Section 5: .env recovery cases A-E -----------------------------------


def _recover_env(tmp_path, *, target=None, legacy=None, old_config_dir="") -> subprocess.CompletedProcess[str]:
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    code_dir = tmp_path / "code"
    code_dir.mkdir(exist_ok=True)
    site_dir = tmp_path / "site"
    site_dir.mkdir(exist_ok=True)
    if target is not None:
        (config_dir / ".env").write_text(target, encoding="utf-8")
    legacy_dir = tmp_path / "legacy-config"
    if legacy is not None:
        legacy_dir.mkdir(exist_ok=True)
        (legacy_dir / ".env").write_text(legacy, encoding="utf-8")
    env = os.environ | {"DF_TEST_MODE": "1"}
    return _run(
        [
            "_test-recover-env",
            _bash_path(config_dir), _bash_path(code_dir), _bash_path(site_dir),
            _bash_path(legacy_dir) if legacy is not None else "",
        ],
        env,
    ), config_dir / ".env"


def test_recover_env_case_a_target_present_no_legacy_keeps_target(tmp_path):
    result, env_file = _recover_env(tmp_path, target="SECRET_KEY=current\n")
    assert result.returncode == 0, result.stderr
    assert env_file.read_text(encoding="utf-8") == "SECRET_KEY=current\n"


def test_recover_env_case_b_legacy_only_is_installed_to_target(tmp_path):
    result, env_file = _recover_env(tmp_path, legacy="SECRET_KEY=legacy\n")
    assert result.returncode == 0, result.stderr
    assert env_file.read_text(encoding="utf-8") == "SECRET_KEY=legacy\n"


def test_recover_env_case_c_identical_files_keep_target(tmp_path):
    result, env_file = _recover_env(
        tmp_path, target="SECRET_KEY=same\n", legacy="SECRET_KEY=same\n"
    )
    assert result.returncode == 0, result.stderr
    assert env_file.read_text(encoding="utf-8") == "SECRET_KEY=same\n"


def test_recover_env_case_d_differing_files_stop(tmp_path):
    result, env_file = _recover_env(
        tmp_path, target="SECRET_KEY=current\n", legacy="SECRET_KEY=different\n"
    )
    assert result.returncode != 0
    assert "differs" in result.stderr
    assert env_file.read_text(encoding="utf-8") == "SECRET_KEY=current\n"


def test_recover_env_case_e_nothing_found_refuses_to_generate_secrets(tmp_path):
    result, env_file = _recover_env(tmp_path)
    assert result.returncode != 0
    assert "refusing to silently generate new secrets" in result.stderr
    assert not env_file.exists()


# --- Section 9: upload merge/conflict handling ----------------------------


def _migrate_uploads(tmp_path) -> tuple[Path, Path]:
    source = tmp_path / "source-uploads"
    dest = tmp_path / "dest-uploads"
    source.mkdir()
    dest.mkdir()
    return source, dest


def test_uploads_copied_when_destination_empty(tmp_path):
    source, dest = _migrate_uploads(tmp_path)
    (source / "invoice.pdf").write_bytes(b"invoice-bytes")
    env = os.environ | {"DF_TEST_MODE": "1"}
    result = _run(["_test-migrate-uploads", _bash_path(source), _bash_path(dest)], env)
    assert result.returncode == 0, result.stderr
    assert (dest / "invoice.pdf").read_bytes() == b"invoice-bytes"


def test_uploads_conflict_on_differing_file_fails_without_overwriting(tmp_path):
    source, dest = _migrate_uploads(tmp_path)
    (source / "invoice.pdf").write_bytes(b"legacy-bytes")
    (dest / "invoice.pdf").write_bytes(b"current-bytes")
    (source / "unique-source-only.pdf").write_bytes(b"unique")
    env = os.environ | {"DF_TEST_MODE": "1"}
    result = _run(["_test-migrate-uploads", _bash_path(source), _bash_path(dest)], env)
    assert result.returncode != 0
    assert "uploads conflict" in result.stderr
    assert (dest / "invoice.pdf").read_bytes() == b"current-bytes"
    assert not (dest / "unique-source-only.pdf").exists()


def test_uploads_identical_files_are_fine_and_unique_files_merge(tmp_path):
    source, dest = _migrate_uploads(tmp_path)
    (source / "shared.pdf").write_bytes(b"same-bytes")
    (dest / "shared.pdf").write_bytes(b"same-bytes")
    (source / "only-in-source.pdf").write_bytes(b"source-only")
    (dest / "only-in-dest.pdf").write_bytes(b"dest-only")
    env = os.environ | {"DF_TEST_MODE": "1"}
    result = _run(["_test-migrate-uploads", _bash_path(source), _bash_path(dest)], env)
    assert result.returncode == 0, result.stderr
    assert (dest / "shared.pdf").read_bytes() == b"same-bytes"
    assert (dest / "only-in-source.pdf").read_bytes() == b"source-only"
    assert (dest / "only-in-dest.pdf").read_bytes() == b"dest-only"


def test_uploads_missing_legacy_directory_is_a_noop(tmp_path):
    dest = tmp_path / "dest-uploads"
    dest.mkdir()
    (dest / "existing.pdf").write_bytes(b"keep-me")
    env = os.environ | {"DF_TEST_MODE": "1"}
    result = _run(
        ["_test-migrate-uploads", _bash_path(tmp_path / "does-not-exist"), _bash_path(dest)], env
    )
    assert result.returncode == 0, result.stderr
    assert (dest / "existing.pdf").read_bytes() == b"keep-me"


# --- Section 6/7/8: migrate-storage explicit --source workflow -----------


def test_migrate_storage_requires_explicit_source(tmp_path):
    paths = _write_install_state(tmp_path)
    (paths["data_dir"]).mkdir(parents=True, exist_ok=True)
    env_file = paths["config_dir"] / ".env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("SECRET_KEY=abc\n", encoding="utf-8")
    env = os.environ | {"DF_TEST_MODE": "1", "DF_STATE_FILE": _bash_path(paths["state"])}
    result = _run(["migrate-storage"], env)
    assert result.returncode != 0
    assert "requires an explicit --source" in result.stderr
    assert "nothing is ever selected automatically" in result.stderr


def test_migrate_storage_rejects_relative_source(tmp_path):
    paths = _write_install_state(tmp_path)
    env_file = paths["config_dir"] / ".env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("SECRET_KEY=abc\n", encoding="utf-8")
    env = os.environ | {"DF_TEST_MODE": "1", "DF_STATE_FILE": _bash_path(paths["state"])}
    result = _run(["migrate-storage", "--source", "relative/production.db"], env)
    assert result.returncode != 0
    assert "must be an absolute path" in result.stderr


def test_migrate_storage_rejects_missing_source(tmp_path):
    paths = _write_install_state(tmp_path)
    env_file = paths["config_dir"] / ".env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("SECRET_KEY=abc\n", encoding="utf-8")
    missing = _bash_path(tmp_path / "nowhere" / "production.db")
    env = os.environ | {"DF_TEST_MODE": "1", "DF_STATE_FILE": _bash_path(paths["state"])}
    result = _run(["migrate-storage", "--source", missing], env)
    assert result.returncode != 0
    assert "must name an existing regular SQLite file" in result.stderr


def test_migrate_storage_rejects_corrupt_source(tmp_path):
    paths = _write_install_state(tmp_path)
    _real_sqlite_backup_tool(paths["code_dir"])
    env_file = paths["config_dir"] / ".env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("SECRET_KEY=abc\n", encoding="utf-8")
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not a sqlite database")
    env = os.environ | {"DF_TEST_MODE": "1", "DF_STATE_FILE": _bash_path(paths["state"])}
    result = _run(["migrate-storage", "--source", _bash_path(corrupt)], env)
    assert result.returncode != 0
    assert "cannot read SQLite database" in result.stderr or "integrity_check failed" in result.stderr


def test_migrate_storage_existing_destination_requires_explicit_preserve_flag(tmp_path):
    from tests.test_deployment import create_db

    paths = _write_install_state(tmp_path)
    _real_sqlite_backup_tool(paths["code_dir"])
    env_file = paths["config_dir"] / ".env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("SECRET_KEY=abc\n", encoding="utf-8")
    destination = paths["data_dir"] / "production.db"
    create_db(destination)
    original = destination.read_bytes()
    source = tmp_path / "legacy.db"
    create_db(source, rows=5)
    env = os.environ | {"DF_TEST_MODE": "1", "DF_STATE_FILE": _bash_path(paths["state"])}

    result = _run(["migrate-storage", "--source", _bash_path(source)], env)
    assert result.returncode != 0
    assert "--preserve-existing-destination" in result.stderr
    assert destination.read_bytes() == original  # untouched


def test_migrate_storage_preserve_flag_is_accepted_before_confirmation(tmp_path):
    """With --preserve-existing-destination, the command must get past the
    'destination already contains a database' refusal and reach the
    confirmation gate instead (verified by the distinct failure reason)."""
    from tests.test_deployment import create_db

    paths = _write_install_state(tmp_path)
    _real_sqlite_backup_tool(paths["code_dir"])
    env_file = paths["config_dir"] / ".env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("SECRET_KEY=abc\n", encoding="utf-8")
    destination = paths["data_dir"] / "production.db"
    create_db(destination)
    source = tmp_path / "legacy.db"
    create_db(source, rows=5)
    env = os.environ | {"DF_TEST_MODE": "1", "DF_STATE_FILE": _bash_path(paths["state"])}

    result = _run(
        ["migrate-storage", "--source", _bash_path(source), "--preserve-existing-destination"],
        env,
    )
    assert result.returncode != 0
    assert "already contains a database" not in result.stderr
    assert "rerun with --yes" in result.stderr


# --- Section 15: doctor --recovery ----------------------------------------


def test_doctor_recovery_reports_no_state_file(tmp_path):
    env = _minimal_env(tmp_path)
    result = _run(["doctor", "--recovery"], env)
    assert result.returncode == 0, result.stderr
    assert "no installation state file found" in result.stderr


def test_doctor_recovery_detects_unsafe_paths_and_never_mutates(tmp_path):
    site_home = tmp_path / "home" / "fixture-user"
    paths = _write_install_state(
        tmp_path, data_dir=site_home / "htdocs" / "figures.example.com" / "data"
    )
    before = paths["state"].read_bytes()
    env = os.environ | {"DF_TEST_MODE": "1", "DF_STATE_FILE": _bash_path(paths["state"])}
    result = _run(["doctor", "--recovery"], env)
    assert result.returncode == 0, result.stderr
    assert "UNSAFE" in result.stderr
    assert paths["state"].read_bytes() == before


def test_doctor_recovery_detects_multiple_database_candidates(tmp_path):
    paths = _write_install_state(tmp_path)
    paths["data_dir"].mkdir(parents=True, exist_ok=True)
    (paths["data_dir"] / "production.db").write_bytes(b"target")
    legacy_db = paths["code_dir"] / "data" / "production.db"
    legacy_db.parent.mkdir(parents=True, exist_ok=True)
    legacy_db.write_bytes(b"legacy")
    env = os.environ | {"DF_TEST_MODE": "1", "DF_STATE_FILE": _bash_path(paths["state"])}
    result = _run(["doctor", "--recovery"], env)
    assert result.returncode == 0, result.stderr
    assert "MULTIPLE DATABASE CANDIDATES" in result.stderr


def test_doctor_recovery_detects_multiple_env_candidates(tmp_path):
    paths = _write_install_state(tmp_path)
    paths["config_dir"].mkdir(parents=True, exist_ok=True)
    (paths["config_dir"] / ".env").write_text("SECRET_KEY=target\n", encoding="utf-8")
    legacy_env = paths["code_dir"] / ".env"
    legacy_env.parent.mkdir(parents=True, exist_ok=True)
    legacy_env.write_text("SECRET_KEY=legacy\n", encoding="utf-8")
    env = os.environ | {"DF_TEST_MODE": "1", "DF_STATE_FILE": _bash_path(paths["state"])}
    result = _run(["doctor", "--recovery"], env)
    assert result.returncode == 0, result.stderr
    assert "MULTIPLE .env CANDIDATES" in result.stderr


# --- Section 10/11: Docker Compose storage safety -------------------------


def test_base_compose_has_no_local_storage_fallback():
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "DAILY_FIGURES_DATA_DIR:?" in text
    assert "DAILY_FIGURES_DATA_DIR:-" not in text
    assert ":/app/data" in text


def test_dev_compose_overlay_exists_and_is_explicit_about_local_storage():
    dev_compose = ROOT / "docker-compose.dev.yml"
    assert dev_compose.is_file()
    text = dev_compose.read_text(encoding="utf-8")
    assert "./data:/app/data" in text
    assert "DAILY_FIGURES_DATA_DIR" in text  # documents that it must still be exported


def test_production_compose_still_mandates_private_paths():
    text = (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
    assert "DAILY_FIGURES_DATA_DIR:?" in text
    assert "DAILY_FIGURES_ENV_FILE:?" in text
    assert "DAILY_FIGURES_DATA_DIR:-" not in text
    assert "DAILY_FIGURES_ENV_FILE:-" not in text


@pytest.mark.skipif(
    subprocess.run(["docker", "compose", "version"], capture_output=True).returncode != 0,
    reason="docker compose is not available in this environment",
)
def test_bare_compose_fails_before_container_creation_without_data_dir():
    env = {k: v for k, v in os.environ.items() if k != "DAILY_FIGURES_DATA_DIR"}
    env["DAILY_FIGURES_ENV_FILE"] = str(ROOT / ".env.example")
    result = subprocess.run(
        ["docker", "compose", "-f", str(ROOT / "docker-compose.yml"), "config"],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "DAILY_FIGURES_DATA_DIR" in result.stderr


@pytest.mark.skipif(
    subprocess.run(["docker", "compose", "version"], capture_output=True).returncode != 0,
    reason="docker compose is not available in this environment",
)
def test_dev_overlay_succeeds_once_data_dir_is_explicitly_exported():
    env = dict(os.environ)
    env["DAILY_FIGURES_DATA_DIR"] = "./data"
    env["DAILY_FIGURES_ENV_FILE"] = str(ROOT / ".env.example")
    result = subprocess.run(
        [
            "docker", "compose",
            "-f", str(ROOT / "docker-compose.yml"),
            "-f", str(ROOT / "docker-compose.dev.yml"),
            "config",
        ],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "/app/data" in result.stdout
