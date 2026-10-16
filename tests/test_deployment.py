"""Deployment-tool tests. CloudPanel cases use representative fixtures."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

import pytest


ROOT = Path(__file__).resolve().parents[1]
BACKUP_TOOL = ROOT / "scripts" / "sqlite_backup.py"
MANAGER = ROOT / "scripts" / "daily-figures"


def run_backup(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(BACKUP_TOOL), *map(str, args)],
        capture_output=True,
        text=True,
    )


def create_db(path: Path, rows: int = 1, wal: bool = False) -> None:
    with sqlite3.connect(path) as conn:
        if wal:
            assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        conn.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT)")
        conn.executemany("INSERT INTO records(value) VALUES (?)", [(f"row-{i}",) for i in range(rows)])


def test_wal_online_backup_is_consistent(tmp_path):
    source = tmp_path / "production.db"
    destination = tmp_path / "backups"
    staging = tmp_path / "private-staging"
    create_db(source, rows=200, wal=True)
    writer_done = threading.Event()

    def writer():
        with sqlite3.connect(source) as conn:
            for index in range(200, 240):
                conn.execute("INSERT INTO records(value) VALUES (?)", (f"row-{index}",))
                conn.commit()
        writer_done.set()

    thread = threading.Thread(target=writer)
    thread.start()
    result = run_backup(
        "create", "--source", source, "--destination", destination,
        "--temporary-directory", staging,
    )
    thread.join(timeout=10)
    assert writer_done.is_set()
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    snapshot = Path(payload["backup"])
    with sqlite3.connect(snapshot) as conn:
        count = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        assert 200 <= count <= 240
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert snapshot.with_name(snapshot.name + ".sha256").is_file()
    assert not list(destination.glob("*.partial"))
    assert not list(staging.glob("*.partial"))


def test_backup_retention_runs_after_success(tmp_path):
    source = tmp_path / "production.db"
    destination = tmp_path / "backups"
    create_db(source)
    for _ in range(4):
        result = run_backup(
            "create", "--source", source, "--destination", destination, "--retention", "2"
        )
        assert result.returncode == 0, result.stderr
    snapshots = list(destination.glob("production-*.db"))
    assert len(snapshots) == 2
    assert all(path.with_name(path.name + ".sha256").exists() for path in snapshots)


def test_corrupt_source_fails_without_published_backup(tmp_path):
    source = tmp_path / "production.db"
    destination = tmp_path / "backups"
    source.write_bytes(b"not sqlite")
    result = run_backup("create", "--source", source, "--destination", destination)
    assert result.returncode != 0
    assert "FATAL" in result.stderr
    assert not list(destination.glob("*.db"))
    assert not list(destination.glob("*.partial"))


def test_copy_refuses_to_overwrite_destination(tmp_path):
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    create_db(source)
    create_db(target)
    original = target.read_bytes()
    result = run_backup("copy", "--source", source, "--destination-file", target)
    assert result.returncode != 0
    assert target.read_bytes() == original


def test_copy_restores_all_records(tmp_path):
    source = tmp_path / "source.db"
    target = tmp_path / "restored.db"
    create_db(source, rows=37, wal=True)
    result = run_backup("copy", "--source", source, "--destination-file", target)
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 37
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def _bash() -> str:
    if os.name == "nt":
        candidate = Path(r"C:\Program Files\Git\bin\bash.exe")
        if candidate.exists():
            return str(candidate)
    return "bash"


def _bash_path(path: Path) -> str:
    if os.name != "nt":
        return str(path)
    result = subprocess.run(
        [_bash(), "-lc", f"cygpath -u '{str(path).replace(chr(39), '')}'"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _write_install_state(tmp_path: Path, *, data_dir: Path | None = None, omit: set[str] | None = None) -> dict[str, Path]:
    omit = omit or set()
    domain = "figures.example.com"
    site_home = tmp_path / "home" / "fixture-user"
    site_dir = site_home / "htdocs" / domain
    code_dir = tmp_path / "opt" / "daily-figures"
    data_dir = data_dir or site_home / "app-data" / "daily-figures"
    config_dir = site_home / "app-config" / "daily-figures"
    backup_root = site_home / "app-backups" / "daily-figures"
    vhost = tmp_path / "nginx" / f"{domain}.conf"
    state = tmp_path / "install.conf"
    for path in (site_dir, code_dir, data_dir, config_dir, backup_root, vhost.parent):
        path.mkdir(parents=True, exist_ok=True)
    vhost.write_text(f"server {{ server_name {domain}; proxy_pass http://127.0.0.1:5000; }}\n")
    values = {
        "DOMAIN": domain,
        "SITE_USER": "fixture-user",
        "SITE_HOME": _bash_path(site_home),
        "SITE_DIR": _bash_path(site_dir),
        "VHOST_FILE": _bash_path(vhost),
        "SITE_UID": "1001",
        "SITE_GID": "1001",
        "CODE_DIR": _bash_path(code_dir),
        "DATA_DIR": _bash_path(data_dir),
        "CONFIG_DIR": _bash_path(config_dir),
        "BACKUP_ROOT": _bash_path(backup_root),
        "PORT": "5000",
        "COMPOSE_FILE": "docker-compose.production.yml",
        "BACKUP_RETENTION": "14",
        "BACKUP_ON_CALENDAR": "*-*-* 04:00:00",
    }
    state.write_text("".join(f"{key}='{value}'\n" for key, value in values.items() if key not in omit))
    return {
        "state": state, "site_home": site_home, "site_dir": site_dir, "code_dir": code_dir,
        "data_dir": data_dir, "config_dir": config_dir, "backup_root": backup_root,
    }


def _load_state(paths: dict[str, Path]) -> subprocess.CompletedProcess[str]:
    env = os.environ | {
        "DF_TEST_MODE": "1",
        "DF_STATE_FILE": _bash_path(paths["state"]),
    }
    return subprocess.run(
        [_bash(), str(MANAGER), "_test-load-state"], env=env, capture_output=True, text=True,
    )


@pytest.mark.parametrize(
    ("domain", "site_user"),
    [("figures.example.com", "fixture-user"), ("stock.other.test", "another-user")],
)
def test_cloudpanel_fixture_discovers_owned_site(tmp_path, domain, site_user):
    nginx = tmp_path / "nginx"
    site = tmp_path / "home" / site_user / "htdocs" / domain
    nginx.mkdir()
    site.mkdir(parents=True)
    (nginx / f"{domain}.conf").write_text(
        f"server {{ server_name {domain}; set $reverse_proxy http://127.0.0.1:5000; proxy_pass $reverse_proxy; }}"
    )
    env = os.environ | {
        "DF_TEST_MODE": "1",
        "DF_TEST_SITE_USER": site_user,
        "DF_HOME_ROOT": _bash_path(tmp_path / "home"),
        "DF_NGINX_DIRS": _bash_path(nginx),
    }
    result = subprocess.run(
        [_bash(), str(MANAGER), "_test-discover-site", domain, "5000"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert site_user in result.stdout
    assert _bash_path(site) in result.stdout


def test_cloudpanel_fixture_rejects_wrong_port(tmp_path):
    domain = "figures.example.com"
    nginx = tmp_path / "nginx"
    site = tmp_path / "home" / "fixture-user" / "htdocs" / domain
    nginx.mkdir()
    site.mkdir(parents=True)
    (nginx / f"{domain}.conf").write_text(
        f"server {{ server_name {domain}; proxy_pass http://127.0.0.1:8000; }}"
    )
    env = os.environ | {
        "DF_TEST_MODE": "1",
        "DF_TEST_SITE_USER": "fixture-user",
        "DF_HOME_ROOT": _bash_path(tmp_path / "home"),
        "DF_NGINX_DIRS": _bash_path(nginx),
        "DF_RUNTIME_DIR": _bash_path(tmp_path / "runtime"),
    }
    result = subprocess.run(
        [_bash(), str(MANAGER), "_test-discover-site", domain, "5000"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "does not point" in result.stderr


def test_cloudpanel_fixture_rejects_missing_site(tmp_path):
    nginx = tmp_path / "nginx"
    home = tmp_path / "home"
    nginx.mkdir()
    home.mkdir()
    env = os.environ | {
        "DF_TEST_MODE": "1",
        "DF_TEST_SITE_USER": "fixture-user",
        "DF_HOME_ROOT": _bash_path(home),
        "DF_NGINX_DIRS": _bash_path(nginx),
    }
    result = subprocess.run(
        [_bash(), str(MANAGER), "_test-discover-site", "missing.example.com", "5000"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "vhost not found" in result.stderr


def test_noninteractive_install_never_prompts_for_missing_values(tmp_path):
    env = os.environ | {
        "DF_TEST_MODE": "1",
        "DF_STATE_FILE": _bash_path(tmp_path / "missing-install.conf"),
    }
    result = subprocess.run(
        [_bash(), str(MANAGER), "install", "--non-interactive"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "requires --domain" in result.stderr


def test_installer_has_exactly_one_interactive_question():
    source = MANAGER.read_text(encoding="utf-8")
    install_body = source.split("install_command() {", 1)[1].split("\nbackup_command()", 1)[0]
    prompt_lines = [line for line in install_body.splitlines() if "read -r" in line]
    assert prompt_lines == [
        "        if [[ -z $domain ]]; then read -r -p 'Enter your CloudPanel domain or subdomain: ' domain; fi"
    ]
    forbidden = ("Installation mode", "backup OnCalendar", "Initial super-admin password")
    assert not any(text in install_body for text in forbidden)


def test_existing_private_database_and_saved_paths_are_preserved(tmp_path):
    paths = _write_install_state(tmp_path)
    database = paths["data_dir"] / "production.db"
    database.write_bytes(b"existing-production-database")

    result = _load_state(paths)

    assert result.returncode == 0, result.stderr
    assert _bash_path(paths["data_dir"]) in result.stdout
    assert _bash_path(paths["config_dir"]) in result.stdout
    assert _bash_path(paths["backup_root"]) in result.stdout
    assert database.read_bytes() == b"existing-production-database"


def test_state_rejects_htdocs_storage_path_mismatch(tmp_path):
    site_home = tmp_path / "home" / "fixture-user"
    paths = _write_install_state(tmp_path, data_dir=site_home / "htdocs" / "figures.example.com" / "data")
    result = _load_state(paths)
    assert result.returncode != 0
    assert "unsafe data path" in result.stderr or "production data path mismatch" in result.stderr


def test_state_requires_production_data_path(tmp_path):
    paths = _write_install_state(tmp_path, omit={"DATA_DIR"})
    result = _load_state(paths)
    assert result.returncode != 0
    assert "DATA_DIR" in result.stderr


def test_private_configuration_rejects_opt_database_fallback(tmp_path):
    paths = _write_install_state(tmp_path)
    legacy = paths["code_dir"] / "data" / "production.db"
    legacy.parent.mkdir()
    legacy.write_bytes(b"must-not-be-copied")

    result = _load_state(paths)

    assert result.returncode != 0
    assert "database location mismatch" in result.stderr
    assert not (paths["data_dir"] / "production.db").exists()
    assert legacy.read_bytes() == b"must-not-be-copied"


def test_conflicting_database_locations_stop_without_overwrite(tmp_path):
    paths = _write_install_state(tmp_path)
    configured = paths["data_dir"] / "production.db"
    configured.write_bytes(b"configured")
    conflicting = paths["site_dir"] / "data" / "production.db"
    conflicting.parent.mkdir()
    conflicting.write_bytes(b"conflicting")

    result = _load_state(paths)

    assert result.returncode != 0
    assert "database location mismatch" in result.stderr
    assert configured.read_bytes() == b"configured"
    assert conflicting.read_bytes() == b"conflicting"


def test_missing_configuration_is_generated_without_replacing_secret(tmp_path):
    code_dir = tmp_path / "code"
    site_home = tmp_path / "home" / "site-user"
    env_file = site_home / "app-config" / "daily-figures" / ".env"
    code_dir.mkdir()
    env_file.parent.mkdir(parents=True)
    env_file.write_text("SECRET_KEY=preserve-this-secret\n", encoding="utf-8")
    env = os.environ | {"DF_TEST_MODE": "1"}
    command = [
        _bash(), str(MANAGER), "_test-repair-env",
        _bash_path(env_file), _bash_path(code_dir), _bash_path(site_home),
    ]
    first = subprocess.run(command, env=env, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    repaired = env_file.read_text(encoding="utf-8")
    assert repaired.count("SECRET_KEY=preserve-this-secret") == 1
    assert "SUPERADMIN_USERNAME=admin" in repaired
    assert "SUPERADMIN_PASSWORD=" in repaired
    second = subprocess.run(command, env=env, capture_output=True, text=True)
    assert second.returncode == 0, second.stderr
    assert env_file.read_text(encoding="utf-8") == repaired


def test_production_compose_fails_closed_when_private_paths_are_undefined():
    text = (ROOT / "docker-compose.production.yml").read_text()
    assert "DAILY_FIGURES_DATA_DIR:?" in text
    assert "DAILY_FIGURES_ENV_FILE:?" in text
    assert "DAILY_FIGURES_DATA_DIR:-" not in text
    assert "DAILY_FIGURES_ENV_FILE:-" not in text
    assert ":/app/data" in text
    assert "\nvolumes:" not in text
    assert "127.0.0.1" in (ROOT / "docker-compose.yml").read_text()


def test_install_layout_is_domain_derived_and_uses_private_cloudpanel_storage():
    source = MANAGER.read_text(encoding="utf-8")
    assert 'DATA_DIR=$SITE_HOME/app-data/daily-figures' in source
    assert 'DATABASE=$DATA_DIR/production.db' in source
    assert 'CONFIG_DIR=$SITE_HOME/app-config/daily-figures' in source
    assert 'BACKUP_ROOT=$SITE_HOME/app-backups/daily-figures' in source
    assert 'DATA_DIR=$SITE_DIR/data' not in source
    assert "BACKUP_RETENTION=14" in source
    assert "BACKUP_ON_CALENDAR='*-*-* 04:00:00'" in source


def test_install_never_auto_copies_or_silently_switches_database_locations():
    source = MANAGER.read_text(encoding="utf-8")
    install_body = source.split("install_command() {", 1)[1].split("\nbackup_command()", 1)[0]
    assert "validate_database_location" in install_body
    assert 'python_tool copy --source "$legacy_source"' not in install_body
    assert 'for candidate in "$CODE_DIR/data/production.db"' not in install_body
    assert "refusing to initialize a replacement" in install_body


def test_installer_rerun_preserves_saved_private_paths_and_existing_database():
    source = MANAGER.read_text(encoding="utf-8")
    install_body = source.split("install_command() {", 1)[1].split("\nbackup_command()", 1)[0]
    state_load = install_body.index("load_state")
    restore_paths = install_body.index("DATA_DIR=$saved_data_dir")
    database_preflight = install_body.index("validate_database_location")
    existing_database = install_body.index('if [[ -f $DATABASE ]]')
    assert state_load < restore_paths < database_preflight < existing_database
    assert 'saved installation database is missing at $DATABASE' in install_body


def test_update_and_lifecycle_commands_use_saved_production_compose_state():
    source = MANAGER.read_text(encoding="utf-8")
    update_body = source.split("update_command() {", 1)[1].split("\nconfirm_destructive()", 1)[0]
    assert "load_state" in update_body
    assert "verify_mount" in update_body
    assert "dc build daily-figures" in update_body
    assert "docker compose" not in update_body
    dc_body = source.split("dc() {", 1)[1].split("\n}", 1)[0]
    assert 'set_compose_environment' in dc_body
    assert '-f "$CODE_DIR/docker-compose.yml" -f "$CODE_DIR/$COMPOSE_FILE"' in dc_body


def test_container_rebuild_keeps_configured_host_directory_bound_to_app_data():
    compose = (ROOT / "docker-compose.production.yml").read_text()
    source = MANAGER.read_text(encoding="utf-8")
    assert "${DAILY_FIGURES_DATA_DIR:?Private persistent data directory is required}:/app/data" in compose
    assert "export DAILY_FIGURES_DATA_DIR=$DATA_DIR" in source
    assert "dc build daily-figures" in source
    assert "dc up -d daily-figures" in source


def test_directory_ownership_keeps_runtime_data_with_cloudpanel_site_user():
    source = MANAGER.read_text(encoding="utf-8")
    body = source.split("prepare_directories() {", 1)[1].split("\n}", 1)[0]
    assert 'chown "$SITE_UID:$SITE_GID" "$DATA_DIR" "$DATA_DIR/uploads"' in body
    assert 'chown root:root "$CONFIG_DIR" "$BACKUP_ROOT"' in body


def test_container_entrypoint_does_not_migrate_or_backup_implicitly():
    text = (ROOT / "docker-entrypoint.sh").read_text()
    executable = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "flask db upgrade" not in executable
    assert "backup_db" not in executable
