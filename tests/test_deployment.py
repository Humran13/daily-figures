"""Deployment-tool tests. CloudPanel cases use representative fixtures."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading


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


def test_cloudpanel_fixture_discovers_owned_site(tmp_path):
    domain = "figures.example.com"
    nginx = tmp_path / "nginx"
    site = tmp_path / "home" / "fixture-user" / "htdocs" / domain
    nginx.mkdir()
    site.mkdir(parents=True)
    (nginx / f"{domain}.conf").write_text(
        f"server {{ server_name {domain}; set $reverse_proxy http://127.0.0.1:5000; proxy_pass $reverse_proxy; }}"
    )
    env = os.environ | {
        "DF_TEST_MODE": "1",
        "DF_TEST_SITE_USER": "fixture-user",
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
    assert "fixture-user" in result.stdout


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
    assert "requires --domain and --mode" in result.stderr


def test_production_compose_requires_explicit_paths():
    text = (ROOT / "docker-compose.production.yml").read_text()
    assert "DAILY_FIGURES_DATA_DIR:?" in text
    assert "DAILY_FIGURES_ENV_FILE:?" in text
    assert "127.0.0.1" in (ROOT / "docker-compose.yml").read_text()


def test_container_entrypoint_does_not_migrate_or_backup_implicitly():
    text = (ROOT / "docker-entrypoint.sh").read_text()
    executable = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "flask db upgrade" not in executable
    assert "backup_db" not in executable
