"""
The original Daily Figures entries table and API — deliberately left as
plain sqlite3, exactly as it worked before this rebuild, so existing
production rows are never at risk. The only change is that the old shared
PIN check is replaced by the new per-user login/roles.

Issued will stop being hand-typed once the dispatch module lands (Phase 4);
until then this endpoint behaves exactly as it always has.
"""
import csv
import io
import os
import sqlite3
from datetime import datetime

from flask import Blueprint, Response, jsonify, request, send_from_directory, current_app

from webapp.auth import current_user, roles_required
from webapp.extensions import db
from webapp.models.user import ROLE_MANAGER, ROLE_OPERATOR, ROLE_SUPER_ADMIN
from webapp.services.audit_service import record_audit

legacy_bp = Blueprint("legacy_entries", __name__)

_DB_PATH = None


def init_legacy_db(db_path):
    global _DB_PATH
    _DB_PATH = db_path
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            shift TEXT NOT NULL,
            product TEXT NOT NULL,
            opening REAL NOT NULL,
            return_val REAL NOT NULL DEFAULT 0,
            production REAL NOT NULL DEFAULT 0,
            issued REAL NOT NULL DEFAULT 0,
            closing REAL NOT NULL,
            notes TEXT DEFAULT '',
            updated_at TEXT NOT NULL,
            UNIQUE(date, shift, product)
        )
    """)
    conn.commit()
    conn.close()


def get_db():
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@legacy_bp.route("/api/entries", methods=["GET"])
def list_entries():
    if current_user() is None:
        return jsonify({"error": "unauthorized"}), 401
    conn = get_db()
    rows = conn.execute("SELECT * FROM entries ORDER BY date, shift, product").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@legacy_bp.route("/api/entries", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_OPERATOR)
def upsert_entry():
    d = request.get_json(force=True)
    required = ["date", "shift", "product", "opening", "return_val", "production", "issued", "closing"]
    for f in required:
        if f not in d:
            return jsonify({"error": f"missing field {f}"}), 400

    conn = get_db()
    before = conn.execute(
        "SELECT * FROM entries WHERE date=? AND shift=? AND product=?",
        (d["date"], d["shift"], d["product"]),
    ).fetchone()
    conn.execute("""
        INSERT INTO entries (date, shift, product, opening, return_val, production, issued, closing, notes, updated_at)
        VALUES (:date, :shift, :product, :opening, :return_val, :production, :issued, :closing, :notes, :updated_at)
        ON CONFLICT(date, shift, product) DO UPDATE SET
            opening=excluded.opening,
            return_val=excluded.return_val,
            production=excluded.production,
            issued=excluded.issued,
            closing=excluded.closing,
            notes=excluded.notes,
            updated_at=excluded.updated_at
    """, {**d, "notes": d.get("notes", ""), "updated_at": datetime.utcnow().isoformat()})
    conn.commit()
    conn.close()

    entity_id = f"{d['date']}|{d['shift']}|{d['product']}"
    record_audit(
        current_user(), "upsert", "entry", entity_id=entity_id,
        before=dict(before) if before else None, after=d,
    )
    db.session.commit()
    return jsonify({"ok": True})


@legacy_bp.route("/api/entries", methods=["DELETE"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_OPERATOR)
def delete_entry():
    d = request.get_json(force=True)
    conn = get_db()
    before = conn.execute(
        "SELECT * FROM entries WHERE date=? AND shift=? AND product=?",
        (d["date"], d["shift"], d["product"]),
    ).fetchone()
    conn.execute("DELETE FROM entries WHERE date=? AND shift=? AND product=?",
                 (d["date"], d["shift"], d["product"]))
    conn.commit()
    conn.close()

    entity_id = f"{d['date']}|{d['shift']}|{d['product']}"
    record_audit(
        current_user(), "delete", "entry", entity_id=entity_id,
        before=dict(before) if before else None, after=None,
    )
    db.session.commit()
    return jsonify({"ok": True})


@legacy_bp.route("/api/export.csv", methods=["GET"])
def export_csv():
    if current_user() is None:
        return jsonify({"error": "unauthorized"}), 401
    conn = get_db()
    rows = conn.execute("SELECT date, shift, product, opening, return_val, production, issued, closing, notes "
                         "FROM entries ORDER BY date, shift, product").fetchall()
    conn.close()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Date", "Shift", "Product", "Opening Stock", "Return", "Production", "Issued", "Closing Stock", "Notes"])
    for r in rows:
        writer.writerow([r["date"], r["shift"], r["product"], r["opening"], r["return_val"],
                          r["production"], r["issued"], r["closing"], r["notes"]])

    record_audit(current_user(), "export_csv", "entry", entity_id=None)
    db.session.commit()

    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=production_log_export.csv"}
    )


@legacy_bp.route("/")
def index():
    return send_from_directory(current_app.static_folder, "index.html")
