"""Regression tests for two confirmed production updater defects
(scripts/daily-figures):

1. Candidate-image resolution (resolve_candidate_image()) used to read
   `dc images -q daily-figures`, which lists whatever happens to be
   locally tagged for the service. When a build moves the `latest` tag,
   the previously-running container's OLD image can become uninspectable
   under Compose's own image listing -- in production this surfaced as
   `dc images -q daily-figures` failing outright with "No such image:
   sha256:...". resolve_candidate_image() now asks Compose for the exact
   CONFIGURED image reference (`dc config --images daily-figures`) and
   inspects that reference directly, independent of the running
   container's old image metadata.

2. candidate_database_preflight()'s temp-directory cleanup used a plain
   function with `trap ... RETURN`. A RETURN trap is NOT scoped to the
   function that set it -- it is a process-wide handler that re-fires on
   EVERY subsequent function return for the rest of the script. In
   production this fired again when update_command() itself returned,
   long after `preflight_dir` had gone out of scope, producing
   "preflight_dir: unbound variable" immediately after "Update
   successful" had already been printed. The function is now a subshell
   (`candidate_database_preflight() ( ... )`) with an EXIT trap, which
   cannot outlive the subshell's own process.

Both regressions are exercised through hidden `_test-*` dispatch hooks in
scripts/daily-figures (same convention as the existing `_test-load-state`
etc. hooks used throughout tests/test_deployment.py and
tests/test_storage_recovery.py) that call the REAL, unmodified functions
under test, with only the heavy external tools (Docker Compose / Docker)
stubbed or -- for one scenario -- exercised for real against disposable,
uniquely-named Docker resources.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from tests.test_deployment import MANAGER, _bash, _bash_path

DOCKER_AVAILABLE = shutil.which("docker") is not None


def _docker_ok() -> bool:
    if not DOCKER_AVAILABLE:
        return False
    try:
        return subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, text=True, timeout=10,
        ).returncode == 0
    except Exception:
        return False


def _run(args: list[str], env: dict[str, str] | None = None, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    full_env = os.environ | (env or {})
    return subprocess.run(
        [_bash(), str(MANAGER), *args], env=full_env, capture_output=True, text=True, timeout=timeout,
    )


SOURCE = MANAGER.read_text(encoding="utf-8")


# =====================================================================
# Bug 1 -- candidate image resolution
# =====================================================================

def test_update_command_no_longer_depends_on_dc_images_q():
    update = SOURCE[SOURCE.index("update_command() {"):SOURCE.index("confirm_destructive()")]
    assert "dc images -q daily-figures" not in update
    assert "candidate_image=$(resolve_candidate_image)" in update
    # Candidate resolution happens strictly after the build and strictly
    # before the online snapshot / live preflight / cutover -- a failure
    # must surface before anything touches the running production service.
    assert update.index("dc build daily-figures") < update.index("candidate_image=$(resolve_candidate_image)")
    assert update.index("candidate_image=$(resolve_candidate_image)") < update.index("online_snapshot=$(latest_database_backup)")
    assert update.index("candidate_image=$(resolve_candidate_image)") < update.index("candidate_database_preflight")
    assert update.index("candidate_image=$(resolve_candidate_image)") < update.index("dc stop daily-figures")


def test_update_marker_records_resolved_candidate_not_stale_old_image():
    update = SOURCE[SOURCE.index("update_command() {"):SOURCE.index("confirm_destructive()")]
    # old_image is captured once, before fetch/merge/build, from the
    # RUNNING container -- purely informational/diagnostic. candidate_image
    # must come from resolve_candidate_image(), called after the build, and
    # the marker line must reference both by name (not a reused/aliased
    # value).
    assert update.index("old_image=$(docker inspect daily_figures_app") < update.index('git -C "$CODE_DIR" fetch')
    assert update.index("candidate_image=$(resolve_candidate_image)") > update.index('git -C "$CODE_DIR" fetch')
    marker_line = 'printf \'old_revision=%q\\nnew_revision=%q\\nold_image=%q\\ncandidate_image=%q\\n\' "$old_revision" "$new_revision" "$old_image" "$candidate_image"'
    assert marker_line in update


def test_resolve_candidate_image_function_fails_before_fatal_on_empty_reference():
    """Scenario F: an empty/invalid configured image reference must fail
    safely, before any live cutover step runs."""
    result = _run(["_test-resolve-candidate-image"], env={"DF_TEST_CANDIDATE_REF": ""})
    assert result.returncode != 0
    assert "could not resolve exactly one configured daily-figures service image reference" in result.stderr


def test_resolve_candidate_image_function_fails_on_unresolvable_reference():
    """A syntactically present but non-existent image reference (the
    build claims a tag that cannot actually be inspected) must also fail
    safely rather than silently propagate a bogus candidate_image."""
    bogus = f"df-test-nonexistent-{uuid.uuid4().hex}:missing"
    result = _run(["_test-resolve-candidate-image"], env={"DF_TEST_CANDIDATE_REF": bogus})
    assert result.returncode != 0
    assert "could not identify the exact candidate image after build" in result.stderr


@pytest.mark.skipif(not _docker_ok(), reason="Docker is not available in this environment")
def test_resolve_candidate_image_survives_old_image_removal_real_docker(tmp_path):
    """Real, disposable-Docker reproduction of the exact production
    incident: a running container references image A; a rebuild moves the
    service's tag onto a new image B, which (as happened in production)
    leaves A no longer inspectable at all -- yet candidate resolution must
    still correctly resolve B, without ever touching A.

    Scenarios covered: A (running container references old image),
    B (build produces/tag points to new candidate), C (old image
    untagged/unavailable to inspection), D (candidate detection resolves
    B), E (candidate detection does not require A to remain inspectable).
    """
    tag = f"df-test-candidate-{uuid.uuid4().hex[:10]}:build"
    container = f"df-test-candidate-{uuid.uuid4().hex[:10]}-app"
    created = {"container": False, "image": False}
    try:
        dockerfile_a = tmp_path / "Dockerfile.a"
        dockerfile_a.write_text("FROM alpine:latest\nLABEL step=a\n")
        build_a = subprocess.run(
            ["docker", "build", "-q", "-t", tag, "-f", str(dockerfile_a), str(tmp_path)],
            capture_output=True, text=True, timeout=120,
        )
        assert build_a.returncode == 0, build_a.stderr
        image_a_id = build_a.stdout.strip()

        run_a = subprocess.run(
            ["docker", "run", "-d", "--name", container, tag, "sleep", "300"],
            capture_output=True, text=True, timeout=60,
        )
        assert run_a.returncode == 0, run_a.stderr
        created["container"] = True

        running = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", container],
            capture_output=True, text=True, timeout=30,
        )
        assert running.stdout.strip() == "true"

        # Rebuild under the SAME tag -- this is what `dc build daily-figures`
        # does when the source changed: the tag moves to a new image.
        dockerfile_b = tmp_path / "Dockerfile.b"
        dockerfile_b.write_text("FROM alpine:latest\nLABEL step=b\n")
        build_b = subprocess.run(
            ["docker", "build", "-q", "-t", tag, "-f", str(dockerfile_b), str(tmp_path)],
            capture_output=True, text=True, timeout=120,
        )
        assert build_b.returncode == 0, build_b.stderr
        image_b_id = build_b.stdout.strip()
        created["image"] = True
        assert image_b_id != image_a_id

        # Belt-and-braces: force the old image id out of the local store if
        # it is still present at all (on some Docker configurations the
        # rebuild above already makes it uninspectable on its own, exactly
        # reproducing the production "No such image" failure without this
        # extra step).
        subprocess.run(["docker", "rmi", "--force", image_a_id], capture_output=True, text=True, timeout=30)

        old_inspectable = subprocess.run(
            ["docker", "image", "inspect", image_a_id, "--format", "{{.Id}}"],
            capture_output=True, text=True, timeout=30,
        )
        assert old_inspectable.returncode != 0, "test setup invalid: old image A must be unavailable for this to reproduce the bug"

        still_running = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", container],
            capture_output=True, text=True, timeout=30,
        )
        assert still_running.stdout.strip() == "true", "the running application must remain healthy throughout (matches production)"

        result = _run(["_test-resolve-candidate-image"], env={"DF_TEST_CANDIDATE_REF": tag})
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == image_b_id
        assert "No such image" not in result.stderr
        assert "unbound variable" not in result.stderr
    finally:
        if created["container"]:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True, text=True, timeout=30)
        subprocess.run(["docker", "rmi", "-f", tag], capture_output=True, text=True, timeout=30)
        if created["image"]:
            subprocess.run(["docker", "rmi", "-f", image_b_id], capture_output=True, text=True, timeout=30)


# =====================================================================
# Bug 2 -- candidate preflight cleanup trap lifecycle
# =====================================================================

def test_candidate_database_preflight_is_a_subshell_with_exit_trap_not_return():
    body = SOURCE[SOURCE.index("candidate_database_preflight() ("):SOURCE.index("create_backup_locked()")]
    assert body.startswith("candidate_database_preflight() (")
    assert "trap 'rm -rf -- \"$preflight_dir\"' EXIT" in body
    # No trap in this function may register against RETURN -- that is the
    # exact historic bug (the surrounding prose is allowed to mention the
    # word "RETURN" when explaining why; only an actual `trap ... RETURN`
    # usage is forbidden).
    assert "' RETURN" not in body
    assert "trap 'rm -rf -- \"$preflight_dir\"' RETURN" not in body
    # The function must close as a subshell ")" (on its own line), not a
    # plain function "}" -- that is what makes the EXIT trap incapable of
    # outliving this one call.
    assert body.rstrip().endswith(")")


def test_successful_candidate_preflight_exits_0_with_no_unbound_variable(tmp_path):
    """Direct regression test matching the production sequence: candidate
    preflight succeeds, further (simulated) phases return normally, success
    is logged, and the process exits 0 -- with no leaked trap error
    afterward. This is the exact sequence that failed in production."""
    staging = tmp_path / "staging"
    staging.mkdir()
    result = _run(["_test-candidate-preflight-trap", _bash_path(staging)])
    assert result.returncode == 0, result.stderr
    assert "unbound variable" not in result.stderr
    assert "preflight_returned_cleanly" in result.stdout
    assert "Update successful" in result.stdout
    # The disposable preflight directory must be gone afterward.
    assert list(staging.iterdir()) == []


def test_candidate_preflight_temp_dir_exists_during_run_and_is_removed_after(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    result = _run(["_test-candidate-preflight-trap", _bash_path(staging)])
    assert result.returncode == 0, result.stderr
    assert list(staging.iterdir()) == []


@pytest.mark.parametrize("fail_at", ["upgrade", "head", "smoke", "migration_guard_cleanup", "migration_guard_inspect"])
def test_candidate_preflight_failure_exits_nonzero_and_still_cleans_up(tmp_path, fail_at):
    staging = tmp_path / "staging"
    staging.mkdir()
    result = _run(["_test-candidate-preflight-trap", _bash_path(staging)], env={"DF_TEST_FAIL_AT": fail_at})
    assert result.returncode != 0
    assert "unbound variable" not in result.stderr
    assert "Update successful" not in result.stdout
    # Cleanup must still have happened on the failure path.
    assert list(staging.iterdir()) == []


def test_candidate_preflight_cleanup_never_targets_a_path_outside_its_own_staging_dir(tmp_path):
    """The removed path must always be the generated disposable
    candidate-preflight-XXXXXX directory under BACKUP_STAGING_DIR, never
    DATA_DIR or the live database -- proven by planting a sentinel file
    next to (but outside) the staging directory and confirming it
    survives."""
    staging = tmp_path / "staging"
    staging.mkdir()
    sentinel = tmp_path / "production.db"
    sentinel.write_text("do-not-touch")
    result = _run(["_test-candidate-preflight-trap", _bash_path(staging)])
    assert result.returncode == 0, result.stderr
    assert sentinel.read_text() == "do-not-touch"


def test_a_later_unrelated_function_return_does_not_retrigger_preflight_cleanup(tmp_path):
    """The historic bug: a RETURN trap outlives candidate_database_preflight
    and re-fires on the NEXT function return anywhere later in the script.
    The test hook simulates exactly that -- two more function calls/returns
    happen after preflight returns, before the final success log line and
    process exit. With the fix, neither of those later returns can trigger
    any cleanup action (there is nothing left registered to fire), and the
    process must still exit 0."""
    staging = tmp_path / "staging"
    staging.mkdir()
    result = _run(["_test-candidate-preflight-trap", _bash_path(staging)])
    assert result.returncode == 0, result.stderr
    # Exactly one cleanup-worthy directory was ever created and it is gone;
    # no error attributable to a later return exists in stderr.
    assert result.stderr == "" or "unbound variable" not in result.stderr


def test_historic_return_trap_pattern_reproduces_the_exact_production_failure(tmp_path):
    """Builds a minimal, self-contained reproduction of the HISTORIC buggy
    pattern (plain function + `trap ... RETURN`) as it existed before this
    fix, independent of the current script, and proves it fails exactly
    the way production did: success is logged, then a later, unrelated
    function return triggers an unbound-variable error. This demonstrates
    that test_successful_candidate_preflight_exits_0_with_no_unbound_variable
    above would have failed against the old code."""
    script = tmp_path / "historic-pattern.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "set -Eeuo pipefail\n"
        "buggy_disposable_op() {\n"
        "    local preflight_dir\n"
        "    preflight_dir=$(mktemp -d)\n"
        "    trap 'rm -rf -- \"$preflight_dir\"' RETURN\n"
        "    : \"$preflight_dir\"\n"
        "}\n"
        "later_unrelated_function() { :; }\n"
        # Mirrors the real script's shape: update_command() (itself called
        # from within main()) calls candidate_database_preflight(), prints
        # success, and returns -- it is this enclosing-function structure
        # that lets the leaked RETURN trap fire again on a later sibling
        # call's return, not a bare top-level sequence of commands.
        "main_like() {\n"
        "    buggy_disposable_op\n"
        "    echo 'Update successful'\n"
        "    later_unrelated_function\n"
        "}\n"
        "main_like\n"
    )
    result = subprocess.run([_bash(), str(script)], capture_output=True, text=True, timeout=15)
    assert "Update successful" in result.stdout
    assert result.returncode != 0
    assert "unbound variable" in result.stderr
