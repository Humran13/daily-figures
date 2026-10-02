from datetime import datetime, timezone

from webapp.extensions import db


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class SparePartImportRow(db.Model):
    """
    Admin/audit-only provenance for a workbook import — never shown in
    normal operator UI. The (source_file, sheet_name, source_row) unique
    constraint is the import's own idempotency key: a source row already
    recorded here is skipped outright on a rerun. raw_json preserves the
    FULL original row (every column, including any unexplained extras
    beyond the standard nine) so nothing is ever silently discarded even
    when its meaning couldn't be established.
    """
    __tablename__ = "spare_part_import_rows"

    id = db.Column(db.Integer, primary_key=True)
    source_file = db.Column(db.String(255), nullable=False)
    sheet_name = db.Column(db.String(80), nullable=False)
    source_row = db.Column(db.Integer, nullable=False)
    raw_json = db.Column(db.Text, nullable=False)
    spare_part_id = db.Column(db.Integer, db.ForeignKey("spare_parts.id"), nullable=True)
    import_batch_id = db.Column(db.String(40), nullable=False)
    created_at = db.Column(db.DateTime(), nullable=False, default=_utcnow)

    __table_args__ = (
        db.UniqueConstraint("source_file", "sheet_name", "source_row", name="uq_spare_part_import_row_source"),
    )
