"""Safety rails around the production update marker (scripts/daily-figures).

These are intentionally source-level checks: the real update path needs Docker,
root, a production database, and a remote, none of which belong in unit tests.
They lock the ordering that makes an interrupted update diagnosable instead of
silently retryable or silently reported as healthy.
"""
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parent.parent / "scripts" / "daily-figures").read_text(encoding="utf-8")


def test_update_refuses_to_start_while_an_incomplete_marker_exists():
    update = SCRIPT[SCRIPT.index("update_command()"):SCRIPT.index("confirm_destructive()")]
    assert '[[ ! -f $RUNTIME_DIR/update-in-progress ]] || fatal' in update
    # The refusal must happen before any backup, fetch, build, or stop.
    marker_check = update.index("update-in-progress")
    assert marker_check < update.index("create_backup_locked")
    assert marker_check < update.index('git -C "$CODE_DIR" fetch')
    assert marker_check < update.index("dc stop daily-figures")


def test_failed_post_migration_integrity_is_recorded_before_fatal():
    update = SCRIPT[SCRIPT.index("update_command()"):SCRIPT.index("confirm_destructive()")]
    assert "post_migration_integrity_failed" in update
    assert update.index("post_migration_integrity_failed") < update.index(
        "post-migration integrity check failed"
    )
    # The marker is retained on every failure path, never removed by a trap.
    assert "trap" not in update
    success_marker = 'mv "$RUNTIME_DIR/update-in-progress" "$RUNTIME_DIR/last-update.conf"'
    assert success_marker in update
    normalize_marker = (
        "sed -i 's/phase=migration_complete_activation_pending/phase=complete/' "
        '"$RUNTIME_DIR/update-in-progress"'
    )
    assert normalize_marker in update
    assert update.index(normalize_marker) < update.index(success_marker)
    # A successful update must never archive a pending/activation marker.
    assert update.index("phase=complete", update.index(normalize_marker)) < update.index(success_marker)


def test_doctor_treats_an_incomplete_marker_as_a_failure():
    doctor = SCRIPT[SCRIPT.index("doctor_command()"):SCRIPT.index("verify_db_command()")]
    marker_check = doctor.index("INCOMPLETE UPDATE")
    assert "failed=1" in doctor[marker_check:]
    assert marker_check < doctor.index("((failed == 0))")
    assert 'fatal "diagnostics found one or more problems"' in doctor
