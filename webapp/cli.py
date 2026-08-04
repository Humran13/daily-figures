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
from webapp.services import legacy_migration as legacy_migration_service


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

    @app.cli.command("audit-legacy-opening-migration")
    def audit_legacy_opening_migration_command():
        """Read-only audit of every legacy `entries` row's Opening Stock
        migration — decodes and reconciles the full spreadsheet equation
        (Opening + Production + Returns - Issued = Closing) for each row,
        cross-references the current DailyFigure/StockAdjustment state
        AND (since the urgent "reset-created zero" correction) any
        `reset_daily_values` AuditLog record for that exact period, and
        classifies every row. A later period's negative Closing is never
        confirmed CLASS_GENUINE_NEGATIVE until every earlier period for
        that same product is proven clean. Never modifies anything.
        Example:

        \b
        flask audit-legacy-opening-migration
        """
        report = legacy_migration_service.audit_opening_migration()
        if not report:
            click.echo("No legacy entries rows found.")
            return

        counts = {}
        for entry in report:
            counts[entry.get("classification", "unknown")] = counts.get(entry.get("classification", "unknown"), 0) + 1

        for entry in report:
            click.echo(
                f"\nentries.id={entry['entries_id']} product={entry.get('product_name')} "
                f"(id={entry.get('product_id')}) date={entry['date']} shift={entry['shift']}"
            )
            if "legacy_opening_label" in entry:
                click.echo(
                    f"  legacy: opening={entry['legacy_opening_label']} closing={entry['legacy_closing_label']} "
                    f"equation_reconciles={entry.get('legacy_equation_reconciles')}"
                )
            if "existing_daily_figure_id" in entry:
                click.echo(
                    f"  current: daily_figure_id={entry['existing_daily_figure_id']} "
                    f"source={entry.get('existing_opening_stock_source')} "
                    f"trusted_anchor={entry.get('existing_trusted_anchor')} "
                    f"opening_base_qty={entry.get('existing_opening_base_qty')} "
                    f"current_closing={entry.get('current_closing_base_qty')}"
                )
            click.echo(f"  CLASSIFICATION: {entry.get('classification')}")
            click.echo(f"  reason: {entry.get('reason')}")
            if entry.get("reset_evidence"):
                re_ = entry["reset_evidence"]
                click.echo(
                    f"  reset evidence: mode={re_['mode']} actor={re_['actor_username']} at={re_['reset_at']} "
                    f"reason=\"{re_['reason']}\""
                )
            if entry.get("classification") in (
                legacy_migration_service.CLASS_MISSING_OPENING_ANCHOR,
                legacy_migration_service.CLASS_MISSING_ANCHOR_AFTER_RESET,
            ):
                click.echo(
                    f"  projected_closing_after_repair={entry.get('projected_closing_after_repair')} "
                    f"({entry.get('projected_closing_after_repair_label')})  "
                    f"delta_from_current={entry.get('current_vs_projected_delta')}"
                )

        click.echo("\n" + "=" * 72)
        click.echo(f"Total legacy rows audited: {len(report)}")
        for classification, count in sorted(counts.items()):
            click.echo(f"  {classification}: {count}")
        repairable = (
            counts.get(legacy_migration_service.CLASS_MISSING_OPENING_ANCHOR, 0)
            + counts.get(legacy_migration_service.CLASS_MISSING_ANCHOR_AFTER_RESET, 0)
        )
        click.echo(
            f"\nRun `flask repair-legacy-opening-migration` (dry run by default) to see the exact repair "
            f"candidates among the {repairable} "
            f"'{legacy_migration_service.CLASS_MISSING_OPENING_ANCHOR}'/"
            f"'{legacy_migration_service.CLASS_MISSING_ANCHOR_AFTER_RESET}' row(s) above."
        )

    @app.cli.command("repair-legacy-opening-migration")
    @click.option("--product-id", "product_id", type=int, default=None, help="Limit to one product (recommended for the first run).")
    @click.option("--apply", "apply_", is_flag=True, default=False, help="Actually apply the repair (default is a dry run).")
    @click.option("--preview-token", "preview_token", default=None, help="The token printed by the dry run — required with --apply.")
    @click.option("--actor-username", "actor_username", default=None, help="An existing user to attribute the repair to — required with --apply.")
    def repair_legacy_opening_migration_command(product_id, apply_, preview_token, actor_username):
        """Repairs ONLY two proven-safe categories: CLASS_MISSING_OPENING_
        ANCHOR (a pure provenance reclassification, opening_stock_source
        -> legacy_migrated_opening, never a quantity change) and, since
        the urgent "reset-created zero" correction,
        CLASS_MISSING_ANCHOR_AFTER_RESET (the same reclassification PLUS
        restoring opening_base_qty/cartons/packs/pieces to the proven
        legacy value, for a row a Reset Daily Values action zeroed).
        Never touches a genuine later manual correction, a reconciliation
        mismatch, a genuine negative legacy balance, a plain reset-created
        zero with nothing to restore, or anything ambiguous. Dry run by
        default.

        \b
        flask repair-legacy-opening-migration --product-id 2
        flask repair-legacy-opening-migration --product-id 2 --preview-token <token> --actor-username root --apply
        """
        if not apply_:
            result = legacy_migration_service.preview_opening_repair(product_id)
            if result["count"] == 0:
                click.echo(
                    "Nothing to repair — no CLASS_MISSING_OPENING_ANCHOR or "
                    "CLASS_MISSING_ANCHOR_AFTER_RESET rows for this scope."
                )
                return
            click.echo(f"DRY RUN — {result['count']} row(s) would be repaired:\n")
            for c in result["candidates"]:
                restores_quantity = c["classification"] == legacy_migration_service.CLASS_MISSING_ANCHOR_AFTER_RESET
                after_opening = c["legacy_base_units"]["opening"] if restores_quantity else c["existing_opening_base_qty"]
                click.echo(
                    f"  entries.id={c['entries_id']} daily_figure_id={c['existing_daily_figure_id']} "
                    f"product={c['product_name']} date={c['date']} shift={c['shift']} "
                    f"[{c['classification']}]"
                )
                click.echo(f"    {c['reason']}")
                click.echo(
                    f"    before: source={c['existing_opening_stock_source']} opening_base_qty={c['existing_opening_base_qty']} "
                    f"current_closing={c['current_closing_base_qty']}"
                )
                click.echo(
                    f"    after:  source=legacy_migrated_opening opening_base_qty={after_opening} "
                    f"projected_closing={c['projected_closing_after_repair']} ({c['projected_closing_after_repair_label']})"
                )
            click.echo(f"\nPreview token: {result['preview_token']}")
            click.echo(
                "No data was changed. To apply, re-run with the exact flags below within the same database state:\n"
                f"  flask repair-legacy-opening-migration"
                f"{f' --product-id {product_id}' if product_id else ''} "
                f"--preview-token {result['preview_token']} --actor-username <you> --apply"
            )
            return

        if not preview_token or not actor_username:
            click.echo("Error: --apply requires both --preview-token (from a prior dry run) and --actor-username.", err=True)
            raise SystemExit(1)

        from webapp.extensions import db as _db
        from webapp.models.user import User
        actor = User.query.filter_by(username=actor_username).first()
        if actor is None:
            click.echo(f"Error: no user named '{actor_username}' exists.", err=True)
            raise SystemExit(1)

        try:
            result = legacy_migration_service.apply_opening_repair(actor, product_id, preview_token)
        except legacy_migration_service.LegacyMigrationRepairConflict as e:
            _db.session.rollback()
            click.echo(f"Error: {e}", err=True)
            raise SystemExit(2)

        _db.session.commit()
        click.echo(f"Repaired {result['count']} row(s):\n")
        for r in result["repaired"]:
            click.echo(
                f"  entries.id={r['entries_id']} daily_figure_id={r['daily_figure_id']} "
                f"product={r['product_name']} date={r['date']} shift={r['shift']} "
                f"-> projected_closing={r['projected_closing_after_repair']}"
            )
        click.echo("\nRe-run `flask stock-ledger` for these products to confirm the repaired ledger.")
