import io

import openpyxl
import pytest

from webapp.services.export_service import build_csv, build_export, build_pdf, build_xlsx

COLUMNS = [("date", "Date"), ("product", "Product"), ("pieces", "Pieces")]
ROWS = [
    {"date": "2026-07-28", "product": "Compact Corporate", "pieces": 124},
    {"date": "2026-07-29", "product": "Lavex", "pieces": 60},
]
TOTALS = {"date": "", "product": "TOTAL", "pieces": 184}


def test_csv_includes_title_filters_generated_by_and_headers():
    out = build_csv(title="My Report", filters={"date_from": "2026-07-01"}, generated_by="alice",
                     columns=COLUMNS, rows=ROWS, totals=TOTALS)
    assert "My Report" in out
    assert "alice" in out
    assert "date_from=2026-07-01" in out
    assert "Date,Product,Pieces" in out
    assert "Compact Corporate,124" in out
    assert "TOTAL,184" in out


def test_csv_no_filters_says_none():
    out = build_csv(title="R", filters={}, generated_by="bob", columns=COLUMNS, rows=[])
    assert "Filters: none" in out


def test_xlsx_round_trips_through_openpyxl():
    out = build_xlsx(title="My Report", filters={"status": "finalized"}, generated_by="alice",
                      columns=COLUMNS, rows=ROWS, totals=TOTALS)
    wb = openpyxl.load_workbook(io.BytesIO(out))
    ws = wb.active
    values = [[c.value for c in row] for row in ws.iter_rows()]
    assert values[0][0] == "My Report"
    assert any("alice" in str(row[0]) for row in values if row[0])
    assert any("status=finalized" in str(row[0]) for row in values if row[0])
    header_row = next(r for r in values if r[0] == "Date")
    assert header_row == ["Date", "Product", "Pieces"]
    assert ["2026-07-28", "Compact Corporate", 124] in values
    assert values[-1] == [None, "TOTAL", 184]  # openpyxl reloads an empty string cell as None


def test_pdf_produces_nonempty_bytes():
    out = build_pdf(title="My Report", filters={}, generated_by="alice", columns=COLUMNS, rows=ROWS)
    assert out.startswith(b"%PDF")
    assert len(out) > 100


def test_build_export_dispatches_by_format():
    for fmt in ("csv", "xlsx", "pdf"):
        content = build_export(fmt, title="T", filters={}, generated_by="x", columns=COLUMNS, rows=ROWS)
        assert content

    with pytest.raises(ValueError):
        build_export("txt", title="T", filters={}, generated_by="x", columns=COLUMNS, rows=ROWS)
