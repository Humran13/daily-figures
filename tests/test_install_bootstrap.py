"""Regression tests for the one-line installation bootstrap."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"


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


def _git(directory: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(directory), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _commit(directory: Path, message: str) -> str:
    _git(directory, "add", "--all")
    _git(directory, "commit", "-m", message)
    return _git(directory, "rev-parse", "HEAD")


def _make_remote(tmp_path: Path, *, tracked_env: bool = False) -> tuple[Path, str, str]:
    publisher = tmp_path / "publisher"
    remote = tmp_path / "daily-figures.git"
    publisher.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(publisher)], check=True, capture_output=True)
    _git(publisher, "config", "user.name", "Bootstrap Test")
    _git(publisher, "config", "user.email", "bootstrap@example.invalid")
    (publisher / "README.md").write_text("old checkout\n", encoding="utf-8")
    if tracked_env:
        (publisher / ".env").write_text("SECRET=preserve-me\n", encoding="utf-8")
    else:
        (publisher / ".gitignore").write_text(
            ".env\ndata/*.db*\ndata/uploads/\ndata/backups/\n", encoding="utf-8"
        )
    old_commit = _commit(publisher, "old release without management utility")
    subprocess.run(["git", "clone", "--bare", str(publisher), str(remote)], check=True, capture_output=True)
    _git(publisher, "remote", "add", "origin", str(remote))

    scripts = publisher / "scripts"
    scripts.mkdir()
    (scripts / "daily-figures").write_text(
        "#!/usr/bin/env bash\n"
        "set -Eeuo pipefail\n"
        "printf '%s\\n' \"$@\" > \"${DAILY_FIGURES_TEST_ARGS_FILE:?}\"\n",
        encoding="utf-8",
        newline="\n",
    )
    if tracked_env:
        (publisher / ".env").unlink()
    new_commit = _commit(publisher, "add management utility")
    _git(publisher, "push", "origin", "main")
    return remote, old_commit, new_commit


def _clone_old(remote: Path, old_commit: str, destination: Path) -> None:
    subprocess.run(["git", "clone", str(remote), str(destination)], check=True, capture_output=True)
    _git(destination, "reset", "--hard", old_commit)
    # Match the POSIX path that Git Bash receives through the bootstrap env.
    _git(destination, "remote", "set-url", "origin", _bash_path(remote))


def _run_installer(
    remote: Path,
    code_dir: Path,
    args_file: Path,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    env = os.environ | {
        "DAILY_FIGURES_BOOTSTRAP_TEST_MODE": "1",
        "DAILY_FIGURES_REPOSITORY_URL": _bash_path(remote),
        "DAILY_FIGURES_CODE_DIR": _bash_path(code_dir),
        "DAILY_FIGURES_TEST_ARGS_FILE": _bash_path(args_file),
    }
    return subprocess.run(
        [_bash(), str(INSTALLER), *arguments],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_existing_old_checkout_fast_forwards_without_moving_production_data(tmp_path):
    remote, old_commit, new_commit = _make_remote(tmp_path)
    code_dir = tmp_path / "opt" / "daily-figures"
    _clone_old(remote, old_commit, code_dir)

    env_file = code_dir / ".env"
    database = code_dir / "data" / "production.db"
    upload = code_dir / "data" / "uploads" / "invoice.pdf"
    backup = code_dir / "data" / "backups" / "production-previous.db"
    env_file.write_bytes(b"SECRET=unchanged\n")
    database.parent.mkdir()
    database.write_bytes(b"legacy sqlite bytes")
    upload.parent.mkdir()
    upload.write_bytes(b"upload bytes")
    backup.parent.mkdir()
    backup.write_bytes(b"backup bytes")
    args_file = tmp_path / "arguments.txt"

    result = _run_installer(
        remote,
        code_dir,
        args_file,
        "--mode",
        "existing",
        "--domain",
        "figures.example.com",
    )

    assert result.returncode == 0, result.stderr
    assert "Management utility is missing" in result.stdout
    assert _git(code_dir, "rev-parse", "HEAD") == new_commit
    assert (code_dir / "scripts" / "daily-figures").is_file()
    assert args_file.read_text().splitlines() == [
        "install",
        "--mode",
        "existing",
        "--domain",
        "figures.example.com",
    ]
    assert env_file.read_bytes() == b"SECRET=unchanged\n"
    assert database.read_bytes() == b"legacy sqlite bytes"
    assert upload.read_bytes() == b"upload bytes"
    assert backup.read_bytes() == b"backup bytes"
    assert not (tmp_path / "home" / "daily-figures" / "data" / "production.db").exists()


@pytest.mark.parametrize("mode", ["fresh", "recovery"])
def test_new_checkout_still_supports_fresh_and_recovery_modes(tmp_path, mode):
    remote, _old_commit, new_commit = _make_remote(tmp_path)
    code_dir = tmp_path / "opt" / "daily-figures"
    args_file = tmp_path / f"{mode}-arguments.txt"

    result = _run_installer(remote, code_dir, args_file, "--mode", mode)

    assert result.returncode == 0, result.stderr
    assert _git(code_dir, "rev-parse", "HEAD") == new_commit
    assert args_file.read_text().splitlines() == ["install", "--mode", mode]


@pytest.mark.parametrize("staged", [False, True])
def test_existing_checkout_refuses_tracked_local_modifications(tmp_path, staged):
    remote, old_commit, _new_commit = _make_remote(tmp_path)
    code_dir = tmp_path / "opt" / "daily-figures"
    _clone_old(remote, old_commit, code_dir)
    readme = code_dir / "README.md"
    readme.write_text("administrator's local change\n", encoding="utf-8")
    if staged:
        _git(code_dir, "add", "README.md")
    args_file = tmp_path / "arguments.txt"

    result = _run_installer(remote, code_dir, args_file, "--mode", "existing")

    assert result.returncode != 0
    assert "modifications exist" in result.stderr
    assert readme.read_text(encoding="utf-8") == "administrator's local change\n"
    assert _git(code_dir, "rev-parse", "HEAD") == old_commit
    assert not (code_dir / "scripts" / "daily-figures").exists()
    assert not args_file.exists()


def test_existing_checkout_refuses_tracked_environment_file_before_fetch(tmp_path):
    remote, old_commit, _new_commit = _make_remote(tmp_path, tracked_env=True)
    code_dir = tmp_path / "opt" / "daily-figures"
    _clone_old(remote, old_commit, code_dir)
    args_file = tmp_path / "arguments.txt"

    result = _run_installer(remote, code_dir, args_file, "--mode", "existing")

    assert result.returncode != 0
    assert "protected production data is tracked" in result.stderr
    assert (code_dir / ".env").read_text(encoding="utf-8") == "SECRET=preserve-me\n"
    assert _git(code_dir, "rev-parse", "HEAD") == old_commit
    assert not args_file.exists()


def test_existing_checkout_refuses_index_flags_that_can_hide_modifications(tmp_path):
    remote, old_commit, _new_commit = _make_remote(tmp_path)
    code_dir = tmp_path / "opt" / "daily-figures"
    _clone_old(remote, old_commit, code_dir)
    _git(code_dir, "update-index", "--skip-worktree", "README.md")
    (code_dir / "README.md").write_text("hidden local change\n", encoding="utf-8")
    args_file = tmp_path / "arguments.txt"

    result = _run_installer(remote, code_dir, args_file, "--mode", "existing")

    assert result.returncode != 0
    assert "skip-worktree/assume-unchanged" in result.stderr
    assert (code_dir / "README.md").read_text(encoding="utf-8") == "hidden local change\n"
    assert _git(code_dir, "rev-parse", "HEAD") == old_commit
    assert not args_file.exists()
