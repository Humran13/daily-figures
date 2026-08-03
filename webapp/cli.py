"""
Internal diagnostic CLI commands. `flask stock-ledger` is a read-only
stock-integrity trace (see webapp/services/stock_ledger_service.py) — it
never writes anything, so it carries no role/permission model of its own;
it is reachable only from wherever the `flask` CLI itself already runs
(server/container shell), never over HTTP, which is the "appropriate
elevated protection" for a diagnostic tool that has no web-facing route
at all.
"""
import click

from webapp.services.stock_ledger_service import (
    LedgerError, LedgerReconciliationError, audit_legacy_adjustments, build_ledger, first_negative_period,
)


def register_cli(app):
    @app.cli.command("stock-ledger")
    @click.option("--product-id", "product_id", required=True, type=int, help="Product ID to trace.")
    @click.option("--from", "date_from", required=True, help="Start date, YYYY-MM-DD.")
    @click.option("--to", "date_to", required=True, help="End date, YYYY-MM-DD (inclusive).")
    @click.option("--shift", "shift", default=None, help="Optional: 'Day' or 'Night' only (default: both).")
    def stock_ledger(product_id, date_from, date_to, shift):
        """Read-only chronological stock ledger for one product — traces
        every base unit behind its Opening/Production/Returns/Issued/
        Closing Stock across a Date+Shift range, without modifying
        anything. Example:

        \b
        flask stock-ledger --product-id 5 --from 2026-07-19 --to 2026-08-03
        """
        try:
            entries = build_ledger(product_id, date_from, date_to, shift=shift)
        except LedgerError as e:
            click.echo(f"Error: {e}", err=True)
            raise SystemExit(1)
        except LedgerReconciliationError as e:
            click.echo(f"RECONCILIATION FAILURE - this diagnostic refuses to print misleading output: {e}", err=True)
            raise SystemExit(2)

        for e in entries:
            click.echo(f"\n{e['date']} {e['shift']} - {e['product_name']} (product_id={e['product_id']})")
            click.echo(f"  Opening:  {e['opening_base_qty']:>8} base units - {e['opening_label']}")
            click.echo(
                f"    source={e['opening_source']} anchor_row_id={e['anchor_row_id']} "
                f"trusted={e['anchor_trusted']} ({e['anchor_trust_reason']})"
            )

            legacy_p = f" (+{e['legacy_production']} legacy-stored)" if e["legacy_production"] else ""
            click.echo(f"  Production: {e['production_total']:>6} base units from {len(e['production_lines'])} line(s){legacy_p}")
            for pl in e["production_lines"]:
                click.echo(
                    f"    record={pl['record_id']} line={pl['line_id']} qty={pl['base_unit_qty']} "
                    f"status={pl['status']} included={pl['included']}"
                )

            note = f" ({e['returns_note']})" if e["returns_note"] else ""
            legacy_r = f" (+{e['legacy_returns']} legacy-stored)" if e["legacy_returns"] else ""
            click.echo(f"  Returns:    {e['returns_total']:>6} base units from {len(e['returns_lines'])} line(s){note}{legacy_r}")
            for rl in e["returns_lines"]:
                click.echo(
                    f"    record={rl['record_id']} line={rl['line_id']} qty={rl['base_unit_qty']} "
                    f"status={rl['status']} included={rl['included']}"
                )

            note = f" ({e['dispatch_note']})" if e["dispatch_note"] else ""
            click.echo(f"  Dispatch:   {e['dispatch_total']:>6} base units from {len(e['dispatch_lines'])} line(s){note}")
            for dl in e["dispatch_lines"]:
                click.echo(
                    f"    record={dl['record_id']} line={dl['line_id']} qty={dl['base_unit_qty']} "
                    f"status={dl['status']} included={dl['included']}"
                )

            click.echo(f"  Adjustments: {e['adjustment_total']:>5} base units from {len(e['adjustments'])} entr{'y' if len(e['adjustments'])==1 else 'ies'}")
            for a in e["adjustments"]:
                click.echo(f"    id={a['id']} delta={a['delta_base_qty']} reason={a['reason']}")

            click.echo(f"  Issued total (dispatch + adjustments): {e['issued_total']}")
            click.echo(f"  Closing:  {e['closing_base_qty']:>8} base units - {e['closing_label']}")
            click.echo(f"  {e['formula']}")

            es = e["entry_status"]
            suffix = f" ({es['completion_type']})" if es["completion_type"] else ""
            click.echo(f"  Entry status: {es['status']}{suffix}")
            if e["review_state"]:
                click.echo(f"  Review: session={e['review_state']['session_status']} product_state={e['review_state']['product_state']}")
            click.echo(f"  Period kind: {e['period_kind']}")
            for w in e["warnings"]:
                click.echo(f"  WARNING: {w}")

        negative = first_negative_period(entries)
        click.echo("\n" + "=" * 72)
        if negative:
            click.echo(
                f"First negative Closing Stock: {negative['date']} {negative['shift']} "
                f"({negative['closing_base_qty']} base units) - period_kind={negative['period_kind']}"
            )
        else:
            click.echo("No negative Closing Stock found in this range.")

    @app.cli.command("audit-legacy-adjustments")
    def audit_legacy_adjustments_command():
        """Read-only inventory of every StockAdjustment created by the
        legacy Issued-figure migration, across every product — never
        modifies anything. No --apply/repair mode exists for this
        command; none is warranted until specific rows are proven
        invalid, duplicated, or wrongly signed (see the completion
        report). Example:

        \b
        flask audit-legacy-adjustments
        """
        rows = audit_legacy_adjustments()
        if not rows:
            click.echo("No legacy-migrated adjustments found.")
            return

        click.echo(f"Found {len(rows)} legacy-migrated adjustment(s):\n")
        flagged = []
        for r in rows:
            click.echo(
                f"adjustment_id={r['adjustment_id']} product_id={r['product_id']} "
                f"({r['product_name']}) date={r['date']} shift={r['shift']}"
            )
            click.echo(f"  delta_base_qty={r['delta_base_qty']}  reason={r['reason']}")
            click.echo(f"  legacy entries.id={r['legacy_entries_id']}  original row={r['legacy_entry_original_value']}")
            click.echo(f"  current closing for this product/date/shift={r['current_closing_base_qty']}")
            click.echo(f"  creates_or_worsens_negative_balance={r['creates_or_worsens_negative_balance']}")
            click.echo(f"  would_be_non_negative_without_this_row={r['would_be_non_negative_without_this_row']}")
            if r["possible_duplicate_lines"]:
                click.echo(f"  POSSIBLE DUPLICATE — matching-quantity source line(s) also exist: {r['possible_duplicate_lines']}")
                flagged.append(r)
            click.echo("")

        click.echo("=" * 72)
        click.echo(f"Proposed action: NONE. {len(flagged)} row(s) have a matching-quantity source line worth a human "
                    f"cross-check; every other row shows no evidence of duplication or a wrong sign. No row is "
                    f"recommended for automatic neutralization — see the completion report for the full policy.")
