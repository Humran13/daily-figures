"""
Daily Figures — self-hosted production log.
Flask + SQLite backend, serves the single-page frontend from /static.
"""
import csv
import io
import os
import sqlite3
from datetime import datetime

from flask import Flask, request, jsonify, session, send_from_directory, Response

app = Flask(__name__, static_folder="static", static_url_path="")
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-key")
APP_PIN = os.environ.get("APP_PIN", "1234")

DB_PATH = os.environ.get("DB_PATH", "/app/data/production.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
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


def require_login():
    return session.get("authed") is True


# ---------- auth ----------
@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(force=True)
    if data.get("pin") == APP_PIN:
        session["authed"] = True
        session.permanent = True
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Wrong PIN"}), 401


@app.route("/api/session", methods=["GET"])
def check_session():
    return jsonify({"authed": require_login()})


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


# ---------- entries ----------
@app.route("/api/entries", methods=["GET"])
def list_entries():
    if not require_login():
        return jsonify({"error": "unauthorized"}), 401
    conn = get_db()
    rows = conn.execute("SELECT * FROM entries ORDER BY date, shift, product").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/entries", methods=["POST"])
def upsert_entry():
    if not require_login():
        return jsonify({"error": "unauthorized"}), 401
    d = request.get_json(force=True)
    required = ["date", "shift", "product", "opening", "return_val", "production", "issued", "closing"]
    for f in required:
        if f not in d:
            return jsonify({"error": f"missing field {f}"}), 400

    conn = get_db()
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
    return jsonify({"ok": True})


@app.route("/api/entries", methods=["DELETE"])
def delete_entry():
    if not require_login():
        return jsonify({"error": "unauthorized"}), 401
    d = request.get_json(force=True)
    conn = get_db()
    conn.execute("DELETE FROM entries WHERE date=? AND shift=? AND product=?",
                 (d["date"], d["shift"], d["product"]))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/export.csv", methods=["GET"])
def export_csv():
    if not require_login():
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
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=production_log_export.csv"}
    )


# ---------- frontend ----------
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
