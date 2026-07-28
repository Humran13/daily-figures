"""
Shared report formatting for every export in the app (dispatch
transactions, daily figures, product movement, customer history,
date-range summaries, filtered search results). One place implements
"title + applied filters + generated date/time + user + headers + totals",
so every export looks and behaves the same way, and quantities are always
written out as exact cartons/packs/pieces columns — never flattened into a
rounded decimal.
"""
import csv
import io
from datetime import datetime, timezone

from openpyxl import Workbook
from openpyxl.styles import Font
from fpdf import FPDF


def _utcnow_str():
    return datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M UTC")


def _filters_line(filters):
    active = {k: v for k, v in (filters or {}).items() if v not in (None, "", [])}
    if not active:
        return "Filters: none"
    return "Filters: " + "; ".join(f"{k}={v}" for k, v in active.items())


def build_csv(*, title, filters, generated_by, columns, rows, totals=None):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([title])
    writer.writerow([f"Generated {_utcnow_str()} by {generated_by}"])
    writer.writerow([_filters_line(filters)])
    writer.writerow([])
    writer.writerow([label for _, label in columns])
    for row in rows:
        writer.writerow([row.get(key, "") for key, _ in columns])
    if totals:
        writer.writerow([totals.get(key, "") for key, _ in columns])
    return buf.getvalue()


def build_xlsx(*, title, filters, generated_by, columns, rows, totals=None):
    wb = Workbook()
    ws = wb.active
    ws.title = "Report"

    bold = Font(bold=True)
    ws.append([title]); ws["A1"].font = bold
    ws.append([f"Generated {_utcnow_str()} by {generated_by}"])
    ws.append([_filters_line(filters)])
    ws.append([])

    header_row_idx = ws.max_row + 1
    ws.append([label for _, label in columns])
    for cell in ws[header_row_idx]:
        cell.font = bold

    for row in rows:
        ws.append([row.get(key, "") for key, _ in columns])

    if totals:
        total_row_idx = ws.max_row + 1
        ws.append([totals.get(key, "") for key, _ in columns])
        for cell in ws[total_row_idx]:
            cell.font = bold

    for i, (_, label) in enumerate(columns, start=1):
        ws.column_dimensions[ws.cell(row=header_row_idx, column=i).column_letter].width = max(12, len(label) + 2)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_pdf(*, title, filters, generated_by, columns, rows, totals=None):
    pdf = FPDF(orientation="L" if len(columns) > 6 else "P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 8, title, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 5, f"Generated {_utcnow_str()} by {generated_by}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, _filters_line(filters), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    table_rows = [[label for _, label in columns]]
    table_rows += [[str(row.get(key, "")) for key, _ in columns] for row in rows]
    if totals:
        table_rows.append([str(totals.get(key, "")) for key, _ in columns])

    pdf.set_font("Helvetica", "", 8)
    with pdf.table(text_align="LEFT") as table:
        for i, data_row in enumerate(table_rows):
            row = table.row()
            for datum in data_row:
                row.cell(datum)

    return bytes(pdf.output())


FORMAT_BUILDERS = {"csv": build_csv, "xlsx": build_xlsx, "pdf": build_pdf}
MIME_TYPES = {
    "csv": "text/csv",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
}


def build_export(fmt, **kwargs):
    if fmt not in FORMAT_BUILDERS:
        raise ValueError(f"unsupported export format '{fmt}' — must be one of {list(FORMAT_BUILDERS)}")
    return FORMAT_BUILDERS[fmt](**kwargs)
