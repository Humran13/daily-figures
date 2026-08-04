"""
Final stock architecture — clean ledger cutover with Excel-style carry
forward.

The existing live history is a mixture of legacy-migrated Opening Stock,
reset-created values, later routine saves, and rows incorrectly promoted
to manual corrections. Rather than repairing that chain one row at a
time, this establishes a controlled new operational starting point: a
LedgerCutover, whose per-product balances come ONLY from an explicitly
entered and verified physical stock count — never derived from (and never
second-guessed by) the pre-cutover history.

Workflow: draft -> verified -> activated (or -> cancelled). Only
activation ever touches live stock data, and it does so by writing one
DailyFigure row per product at the cutover's exact effective Date+Shift
with opening_stock_source=OPENING_STOCK_SOURCE_LEDGER_CUTOVER — an
UNCONDITIONALLY TRUSTED anchor (see webapp/models/daily_figure.py). This
is the entire mechanism: stock_service.py's existing backward-scanning
anchor logic (_find_anchor_figure()/get_prior_closing_base_qty(), used by
literally every stock surface in the app — Daily Figures, Dashboard,
Reports, History, Exports, Reset, the stock-ledger CLI) already stops at
the FIRST trusted anchor it finds walking backward from a target period.
Placing the cutover row there means every post-cutover period's carry-
forward search simply never reaches pre-cutover data — no other function
in the app needed to change. "One shared ledger service used by every
screen" was already true before this file existed; this activates a new,
verified boundary within it, deliberately, minimally, and low-risk.
"""
import hashlib
from datetime import datetime, timezone

from webapp.extensions import db
from webapp.models.daily_figure import (
    OPENING_STOCK_SOURCE_LEDGER_CUTOVER,
    DailyFigure,
)
from webapp.models.ledger_cutover import (
    STATUS_ACTIVATED,
    STATUS_CANCELLED,
    STATUS_DRAFT,
    STATUS_VERIFIED,
    LedgerCutover,
    LedgerCutoverBalance,
)
from webapp.models.product import Product
from webapp.models.user import ROLE_MANAGER, ROLE_SUPER_ADMIN
from webapp.services.packaging import PackagingError, normalize, to_base_units


class LedgerCutoverError(ValueError):
    """User-facing validation problem — mapped to HTTP 400 by routes."""


class LedgerCutoverConflict(ValueError):
    """The cutover's balances (or its status) changed since the dry-run
    preview, or no valid token was supplied — never activated silently
    against different data than what was previewed."""


def _require_elevated(actor):
    if actor is None or actor.role not in (ROLE_MANAGER, ROLE_SUPER_ADMIN):
        raise LedgerCutoverError("Only a Manager or Super Administrator may manage a ledger cutover")


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def create_draft(effective_date, effective_shift, reason, actor):
    """Starts a new cutover in DRAFT status. Draft cutovers are completely
    inert — they never affect live stock math (only an ACTIVATED cutover
    does, see activate_cutover() below)."""
    _require_elevated(actor)
    if not effective_date:
        raise LedgerCutoverError("effective_date is required")
    if effective_shift not in ("Day", "Night"):
        raise LedgerCutoverError("effective_shift must be Day or Night")
    if not reason:
        raise LedgerCutoverError("A reason is required to start a ledger cutover")

    cutover = LedgerCutover(
        effective_date=effective_date, effective_shift=effective_shift,
        status=STATUS_DRAFT, reason=reason, created_by=actor.id,
    )
    db.session.add(cutover)
    db.session.flush()

    from webapp.services.audit_service import record_audit
    record_audit(actor, "create_ledger_cutover_draft", "ledger_cutover", entity_id=cutover.id, after=cutover.to_dict())
    return cutover


def get_cutover(cutover_id):
    return db.session.get(LedgerCutover, cutover_id)


def list_cutovers():
    return LedgerCutover.query.order_by(LedgerCutover.effective_date.desc(), LedgerCutover.id.desc()).all()


def get_active_cutover(on_or_before_date=None, on_or_before_shift=None):
    """The most recent ACTIVATED cutover whose effective (date, shift) is
    at or before the given period — or the globally most recently
    activated cutover if no period is given. None if no cutover has ever
    been activated. Read-only, cheap (small table, filtered query)."""
    from webapp.services.stock_service import _sort_key

    activated = LedgerCutover.query.filter_by(status=STATUS_ACTIVATED).all()
    if not activated:
        return None
    if on_or_before_date is None:
        return max(activated, key=lambda c: _sort_key(c.effective_date, c.effective_shift))
    target = _sort_key(on_or_before_date, on_or_before_shift)
    candidates = [c for c in activated if _sort_key(c.effective_date, c.effective_shift) <= target]
    if not candidates:
        return None
    return max(candidates, key=lambda c: _sort_key(c.effective_date, c.effective_shift))


def set_balance(cutover_id, product_id, cartons, packs, pieces, actor):
    """Enters (or updates) one product's verified physical-count balance
    on a DRAFT cutover. Zero is valid. A negative cartons/packs/pieces
    value is rejected outright by packaging.normalize()/to_base_units()
    (PackagingError) — re-raised here as LedgerCutoverError. Converts
    immediately to exact integer base units using the product's CURRENT
    packaging rule; that snapshot is never re-interpreted later even if
    the rule subsequently changes (same "never reinterpret history"
    guarantee as every other quantity-entry point in this app)."""
    _require_elevated(actor)
    cutover = get_cutover(cutover_id)
    if cutover is None:
        raise LedgerCutoverError("cutover does not exist")
    if cutover.status != STATUS_DRAFT:
        raise LedgerCutoverError(f"cutover is '{cutover.status}' — balances can only be entered while it is 'draft'")

    product = db.session.get(Product, product_id)
    if product is None:
        raise LedgerCutoverError("product does not exist")
    rule = product.current_packaging_rule()
    if rule is None:
        raise LedgerCutoverError(f"'{product.name}' has no packaging rule configured yet")

    try:
        oc, op_, opc = normalize(cartons, packs, pieces, rule)
        base_qty = to_base_units(oc, op_, opc, rule)
    except PackagingError as e:
        raise LedgerCutoverError(str(e)) from e

    balance = LedgerCutoverBalance.query.filter_by(cutover_id=cutover.id, product_id=product.id).first()
    if balance is None:
        balance = LedgerCutoverBalance(cutover_id=cutover.id, product_id=product.id, created_by=actor.id)
        db.session.add(balance)
    balance.cartons, balance.packs, balance.pieces, balance.base_qty = oc, op_, opc, base_qty
    balance.updated_by = actor.id
    db.session.flush()
    return balance


def _active_products():
    return Product.query.filter_by(active=True).order_by(Product.display_order, Product.name).all()


def cutover_status_report(cutover_id):
    """Read-only: every active product, its entered balance (if any), and
    whether the cutover is complete enough to verify/activate. Used by
    both the preview API/CLI and verify_cutover()'s own completeness
    check — a single shared source of truth for "is this cutover ready,"
    never two competing completeness checks."""
    cutover = get_cutover(cutover_id)
    if cutover is None:
        raise LedgerCutoverError("cutover does not exist")

    from webapp.services import stock_service as svc

    balances_by_product = {b.product_id: b for b in cutover.balances}
    rows = []
    missing = []
    for product in _active_products():
        rule = product.current_packaging_rule()
        balance = balances_by_product.get(product.id)
        row = {
            "product_id": product.id, "product_name": product.name,
            "packaging_rule": rule.to_dict() if rule else None,
            "has_balance": balance is not None,
            "cartons": balance.cartons if balance else None,
            "packs": balance.packs if balance else None,
            "pieces": balance.pieces if balance else None,
            "base_qty": balance.base_qty if balance else None,
            "label": svc.qty_label_signed(balance.base_qty, rule) if balance and rule else None,
        }
        if balance is None:
            missing.append({"product_id": product.id, "product_name": product.name})
        rows.append(row)

    return {
        "cutover": cutover.to_dict(),
        "products": rows,
        "missing_products": missing,
        "ready": len(missing) == 0,
    }


def verify_cutover(cutover_id, actor):
    """Marks a draft cutover VERIFIED — requires every active product to
    have a balance (an active product with no entered balance blocks
    verification; deactivate it first via Admin > Products if it should
    be deliberately excluded — no separate "excluded from cutover" flag
    is introduced). Still completely inert: verification alone never
    touches live stock data."""
    _require_elevated(actor)
    cutover = get_cutover(cutover_id)
    if cutover is None:
        raise LedgerCutoverError("cutover does not exist")
    if cutover.status != STATUS_DRAFT:
        raise LedgerCutoverError(f"cutover is '{cutover.status}' — only a 'draft' cutover can be verified")

    report = cutover_status_report(cutover_id)
    if not report["ready"]:
        names = ", ".join(p["product_name"] for p in report["missing_products"])
        raise LedgerCutoverError(f"every active product needs a balance before verification — missing: {names}")

    cutover.status = STATUS_VERIFIED
    cutover.verified_by = actor.id
    cutover.verified_at = _utcnow()
    db.session.flush()

    from webapp.services.audit_service import record_audit
    record_audit(actor, "verify_ledger_cutover", "ledger_cutover", entity_id=cutover.id, after=cutover.to_dict())
    return cutover


def cancel_cutover(cutover_id, reason, actor):
    """Cancels a draft or verified cutover — never an activated one (an
    activated cutover's DailyFigure rows are real, live, unconditionally-
    trusted anchors; cancelling the LedgerCutover record itself would not
    un-write them, so this is deliberately refused to avoid a
    misleadingly 'cancelled' cutover that still governs live stock)."""
    _require_elevated(actor)
    cutover = get_cutover(cutover_id)
    if cutover is None:
        raise LedgerCutoverError("cutover does not exist")
    if cutover.status not in (STATUS_DRAFT, STATUS_VERIFIED):
        raise LedgerCutoverError(f"cutover is '{cutover.status}' — only 'draft' or 'verified' can be cancelled")
    if not reason:
        raise LedgerCutoverError("A reason is required to cancel a ledger cutover")

    cutover.status = STATUS_CANCELLED
    cutover.cancelled_by = actor.id
    cutover.cancelled_at = _utcnow()
    db.session.flush()

    from webapp.services.audit_service import record_audit
    record_audit(actor, "cancel_ledger_cutover", "ledger_cutover", entity_id=cutover.id,
                 before={"reason_for_cancellation": reason}, after=cutover.to_dict())
    return cutover


def _fingerprint(cutover):
    """A stable fingerprint of the VERIFIED cutover's exact status and
    balance set — the preview/activation token. Recomputing it at apply
    time and requiring an exact match is what makes activation refuse to
    run against balances (or a status) that changed since the dry run."""
    parts = [f"cutover:{cutover.id}:{cutover.status}:{cutover.updated_at.isoformat() if cutover.updated_at else ''}"]
    for b in sorted(cutover.balances, key=lambda b: b.product_id):
        parts.append(f"{b.product_id}:{b.base_qty}:{b.updated_at.isoformat() if b.updated_at else ''}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def preview_activation(cutover_id):
    """Dry run — never modifies anything. Returns the full report plus
    the token a subsequent activate_cutover() call must be given back
    verbatim, and the projected first Closing Stock / next Opening Stock
    per product (assuming no post-cutover movement has been recorded
    yet — this is a projection, not a guarantee, since activation itself
    is what actually fixes the balance in place)."""
    cutover = get_cutover(cutover_id)
    if cutover is None:
        raise LedgerCutoverError("cutover does not exist")
    if cutover.status != STATUS_VERIFIED:
        raise LedgerCutoverError(f"cutover is '{cutover.status}' — only a 'verified' cutover can be activated")

    report = cutover_status_report(cutover_id)

    from webapp.services import stock_service as svc
    for row in report["products"]:
        product = db.session.get(Product, row["product_id"])
        rule = product.current_packaging_rule()
        opening = row["base_qty"] or 0
        production = svc.production_base_qty(product.id, cutover.effective_date, cutover.effective_shift)
        returns = svc.return_base_qty(product.id, cutover.effective_date, cutover.effective_shift)
        issued = svc.issued_base_qty(product.id, cutover.effective_date, cutover.effective_shift)
        projected_closing = svc.compute_closing(opening, production, returns, issued)
        row["projected_first_closing_base_qty"] = projected_closing
        row["projected_first_closing_label"] = svc.qty_label_signed(projected_closing, rule)

    existing_overlap = LedgerCutover.query.filter(
        LedgerCutover.status == STATUS_ACTIVATED,
    ).all()
    report["existing_active_cutovers"] = [c.to_dict() for c in existing_overlap]
    report["preview_token"] = _fingerprint(cutover)
    return report


def parse_cutover_csv(content):
    """
    Optional convenience import — the database remains authoritative;
    this only pre-fills what a Manager would otherwise type by hand, and
    NEVER activates anything by itself (see import_cutover_csv() below,
    which only stages rows via the same set_balance() a manual entry
    uses). Required columns: product_name or product_id, cartons, packs,
    pieces. Validates every row: unknown product, duplicate product,
    invalid packaging tier (a value that doesn't fit the product's own
    rule, or a nonzero packs value for a no-pack-tier product), and a
    negative quantity are all reported as per-row errors, never silently
    skipped or guessed. Returns {"rows": [...], "errors": [...]} — a row
    with any error is excluded from `rows` and reported in `errors`
    instead; the caller must show a complete preview before staging.
    """
    import csv
    import io

    reader = csv.DictReader(io.StringIO(content))
    fieldnames = [f.strip().lower() for f in (reader.fieldnames or [])]
    if "cartons" not in fieldnames or ("product_name" not in fieldnames and "product_id" not in fieldnames):
        return {"rows": [], "errors": [{"row": 0, "error": "CSV must have a 'cartons' column and either 'product_name' or 'product_id'"}]}

    rows, errors = [], []
    seen_product_ids = set()
    for i, raw in enumerate(reader, start=2):  # header is row 1
        raw = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        product = None
        if raw.get("product_id"):
            try:
                product = db.session.get(Product, int(raw["product_id"]))
            except ValueError:
                pass
        elif raw.get("product_name"):
            product = Product.query.filter_by(name=raw["product_name"]).first()

        if product is None:
            errors.append({"row": i, "error": f"unknown product: {raw.get('product_name') or raw.get('product_id')}"})
            continue
        if product.id in seen_product_ids:
            errors.append({"row": i, "error": f"duplicate row for product '{product.name}'"})
            continue

        rule = product.current_packaging_rule()
        if rule is None:
            errors.append({"row": i, "error": f"'{product.name}' has no packaging rule configured"})
            continue

        try:
            cartons = int(raw.get("cartons") or 0)
            packs = int(raw.get("packs") or 0)
            pieces = int(raw.get("pieces") or 0)
            oc, op_, opc = normalize(cartons, packs, pieces, rule)
            base_qty = to_base_units(oc, op_, opc, rule)
        except (ValueError, PackagingError) as e:
            errors.append({"row": i, "error": f"'{product.name}': {e}"})
            continue

        seen_product_ids.add(product.id)
        rows.append({
            "product_id": product.id, "product_name": product.name,
            "cartons": oc, "packs": op_, "pieces": opc, "base_qty": base_qty,
        })

    return {"rows": rows, "errors": errors}


def import_cutover_csv(cutover_id, content, actor):
    """Stages every valid row from parse_cutover_csv() onto a DRAFT
    cutover via the SAME set_balance() a manual entry uses — never a
    second, competing write path, and never activates anything. Returns
    the parse report plus how many rows were actually staged."""
    report = parse_cutover_csv(content)
    staged = []
    for row in report["rows"]:
        balance = set_balance(cutover_id, row["product_id"], row["cartons"], row["packs"], row["pieces"], actor)
        staged.append(balance.to_dict())
    return {"errors": report["errors"], "staged": staged, "staged_count": len(staged)}


def activate_cutover(cutover_id, actor, preview_token=None, confirmation_text=None,
                      backup_confirmed=False, reason=None):
    """
    Applies the cutover — the ONLY function in this module that ever
    touches live stock data. Atomic (single flush; the caller's route/CLI
    commits once at the end and the whole session rolls back on any
    exception here), idempotent by construction (an already-ACTIVATED
    cutover refuses a second activation outright), token-gated (refuses
    if the verified balance set changed since the preview), and refuses
    if ANY active product is missing a balance.

    Only one ACTIVATED cutover may govern a given chronological period —
    activating a new cutover whose effective period is NOT strictly after
    every existing activated cutover's own effective period is refused;
    this is never silently resolved, exactly as required.
    """
    _require_elevated(actor)
    if not reason:
        raise LedgerCutoverError("A reason is required to activate a ledger cutover")
    if not backup_confirmed:
        raise LedgerCutoverError("Explicit backup confirmation is required before activating a ledger cutover")

    cutover = get_cutover(cutover_id)
    if cutover is None:
        raise LedgerCutoverError("cutover does not exist")
    if cutover.status == STATUS_ACTIVATED:
        raise LedgerCutoverError("cutover is already activated — activation is not re-applied")
    if cutover.status != STATUS_VERIFIED:
        raise LedgerCutoverError(f"cutover is '{cutover.status}' — only a 'verified' cutover can be activated")

    expected_confirmation = f"ACTIVATE LEDGER CUTOVER {cutover.effective_date} {cutover.effective_shift.upper()}"
    if confirmation_text != expected_confirmation:
        raise LedgerCutoverError(f'Activation requires typed confirmation matching exactly: "{expected_confirmation}"')

    current_token = _fingerprint(cutover)
    if not preview_token or current_token != preview_token:
        raise LedgerCutoverConflict(
            "The cutover's balances (or status) changed since the preview (or no valid --preview-token was "
            "supplied) — run the dry run again and pass its exact token."
        )

    report = cutover_status_report(cutover_id)
    if not report["ready"]:
        names = ", ".join(p["product_name"] for p in report["missing_products"])
        raise LedgerCutoverError(f"every active product needs a balance before activation — missing: {names}")

    from webapp.services.stock_service import _sort_key
    target_key = _sort_key(cutover.effective_date, cutover.effective_shift)
    for other in LedgerCutover.query.filter_by(status=STATUS_ACTIVATED).all():
        if _sort_key(other.effective_date, other.effective_shift) >= target_key:
            raise LedgerCutoverError(
                f"an activated cutover (id={other.id}, effective {other.effective_date} {other.effective_shift}) "
                "already governs this or a later period — a replacement requires cancelling/superseding it "
                "through an explicit, separate action, never a silent overwrite"
            )

    from webapp.services.audit_service import record_audit

    written = []
    for balance in cutover.balances:
        product = db.session.get(Product, balance.product_id)
        rule = product.current_packaging_rule()
        figure = DailyFigure.query.filter_by(
            product_id=product.id, date=cutover.effective_date, shift=cutover.effective_shift,
        ).first()
        before = None
        if figure is None:
            figure = DailyFigure(
                product_id=product.id, date=cutover.effective_date, shift=cutover.effective_shift,
                created_by=actor.id, packaging_rule_id=rule.id,
            )
            db.session.add(figure)
        else:
            before = {
                "opening_base_qty": figure.opening_base_qty, "opening_stock_source": figure.opening_stock_source,
            }
        figure.opening_cartons, figure.opening_packs, figure.opening_pieces = balance.cartons, balance.packs, balance.pieces
        figure.opening_base_qty = balance.base_qty
        figure.opening_stock_source = OPENING_STOCK_SOURCE_LEDGER_CUTOVER
        figure.opening_stock_is_override = True
        figure.cutover_id = cutover.id
        figure.packaging_rule_id = rule.id
        figure.updated_by = actor.id
        db.session.flush()

        record_audit(
            actor, "activate_ledger_cutover_balance", "daily_figure", entity_id=figure.id,
            before=before,
            after={"opening_base_qty": figure.opening_base_qty, "opening_stock_source": figure.opening_stock_source, "cutover_id": cutover.id},
        )
        written.append({"product_id": product.id, "product_name": product.name, "daily_figure_id": figure.id, "opening_base_qty": balance.base_qty})

    cutover.status = STATUS_ACTIVATED
    cutover.activated_by = actor.id
    cutover.activated_at = _utcnow()
    db.session.flush()

    record_audit(
        actor, "activate_ledger_cutover", "ledger_cutover", entity_id=cutover.id,
        before={"status": STATUS_VERIFIED}, after={**cutover.to_dict(), "reason_for_activation": reason, "products_written": len(written)},
    )
    return {"cutover": cutover.to_dict(), "products_written": written, "count": len(written)}
