# Spare Parts: Consumption & Procurement Planning

Answers "if we source spare parts from China for the next N months, what
should we buy, how much, which is most urgent, and roughly what will it
cost?" — `/spare-parts-planning.html`, `GET /api/spare-parts/planning`.

## Where the numbers come from

Consumption is **only** genuine Stock Out activity: `SparePartMovement`
rows with `movement_type=STOCK_OUT` and `status=active`. Adjustments
(corrections/counts), opening balances, receipts (Stock In), and voided
rows never count — they would otherwise inflate or distort real demand.
Current stock is the same authoritative ledger derivation the rest of the
app already uses (`spare_part_movement_service.current_stock()` /
`bulk_current_stock()`), never the denormalized cache.

## Average monthly consumption

```
average_monthly_consumption = total_quantity_in_window / max(1, window_days / 30)
```

The floor at "1 month-equivalent" is deliberate: without it, a part with
only a few days of history would have its usage scaled up by as much as
~30x into a wildly exaggerated monthly rate. A full year of history
divides by ~12.17 (365/30), not a flat 12, to stay internally consistent.
`window_days` is capped to the part's own observable ledger history (or
its `created_at` date if it has none yet), so a brand-new part is never
treated as if it had a full historical period.

## Trend (Increasing / Decreasing / Stable / Insufficient Data)

Compares two **equal-length, non-overlapping** periods: the latest half of
the selected historical analysis period vs. the preceding half (e.g.
latest 90 days vs. preceding 90 days, for a 180-day analysis period).
`Insufficient Data` when the part's history doesn't reach back far enough
to populate the preceding half. A ±15% change (`TREND_THRESHOLD_PERCENT`)
is the minimum to call it Increasing/Decreasing rather than Stable, so
small day-to-day noise doesn't flip the label constantly.

## Forecast, safety stock, incoming stock

`forecast_monthly_demand()` is a one-line, intentionally pluggable seam —
today it simply returns `average_monthly_consumption`, but a future,
smarter method can replace its body without touching the route or page.

**Safety stock** is DELIBERATELY separate from `minimum_stock`.
`minimum_stock` is the existing reorder-level/low-stock-alert field
(`is_low_stock()`, `stock_status_for()`) and keeps meaning exactly that —
unchanged by this feature. Safety stock has its own nullable column,
`safety_stock_override` (migration `d4f6a8b0c2e4`): when a part has one
set, it is used as-is; when `NULL` (every part by default), the
application default applies instead — 1 month of that part's own average
monthly consumption. `safety_stock_for()` never reads `minimum_stock`.

**Confirmed incoming stock** is always `0` for the forecast math — this
application has no purchase-order/pending-receipt concept yet. It is its
own function (`confirmed_incoming_stock_for()`) specifically so a real
source can be wired in later without touching the recommended-purchase
formula. The UI/export-facing value is never a bare "0", which would
wrongly imply a verified absence of pending orders — `incoming_stock_display()`
always shows **"Not tracked"** while `INCOMING_STOCK_TRACKED` is `False`
(flipping that one flag, once a real source exists, is the only change
needed to show the real quantity instead).

## Recommended purchase

```
recommended_purchase = projected_demand + safety_stock
                        - current_usable_stock - confirmed_incoming_stock
```

Clamped to a minimum of zero. Computed for the fixed horizons (3/6/9/12
months) and separately for whatever horizon is currently selected
(3/6/9/12/custom).

## Stock coverage / estimated stockout

`stock_coverage_months = current_stock / forecast_monthly_demand` — `None`
(never Infinity/NaN) when demand is zero; the UI shows "No recent
consumption". The estimated stockout date is a **planning estimate**,
labeled as such, and never projected more than 10 years out.

## ABC classification

Prefers **consumption-value** ABC (`annual_quantity x current unit cost`,
Pareto cutoffs at the first ~80% of cumulative value = A, next ~15% = B,
remaining ~5% = C). Falls back to a **quantity-only** ranking for the
*entire* report — never a per-row mix, which would not be one defensible
method — whenever fewer than 50% of the in-scope rows have a usable unit
cost (the threshold is inclusive: exactly 50% still uses value-based ABC).

A missing price is never treated as zero value, and — design review
correction — is never silently folded into class C either. Under
value-based ABC, a row with no usable cost is explicitly
**`Unclassified`**, never `C`: `C` asserts "genuinely low consumption
value", a claim this method cannot make about a row whose value it
cannot compute, no matter how large that row's quantity is. Under the
quantity-based fallback every row is ranked regardless of pricing, so
`Unclassified` never appears there.

## Procurement priority

A deterministic rule cascade (`procurement_priority()` — first match
wins), combining stock position, projected coverage, criticality, and
trend. A low-volume part can still reach **Urgent**/**High** purely
through being marked Critical — criticality influences priority without
ever touching historical consumption numbers.

## Critical parts

**Super Administrator only** may mark a part Critical (with an optional
note) via the existing `PATCH /api/spare-parts/<id>` endpoint — already
audited by that route. This is restricted the same way pricing is
(`_critical_forbidden_response()`), because Critical status drives
procurement priority directly, even though Manager may still edit this
same spare part's other fields (name, category, `minimum_stock`,
`safety_stock_override`, machine associations, ...) — a request that
bundles `critical`/`critical_note` with other fields is rejected as a
whole, never partially applied. *Viewing* Critical status follows normal
Spare Parts section access and is visible to every role — only *changing*
it is restricted.

## Slow Moving / Dead Stock

**Dead Stock**: stock on hand, the part is at least 365 days old, *and*
there has been no active Stock Out in that same window. A brand-new part
with stock and simply no sign-outs yet is never Dead Stock — just new.
**Slow Moving**: has *some* recent consumption but current stock covers
more than 12 months of its own demand.

## China / Local / Best Price

`procurement_cost = recommended_quantity x unit_buying_price`. **Best
Price** picks the cheaper *valid* source per row; a missing price is never
substituted with the other source's price or with zero — it is shown as
missing. Savings are only reported when both China and Local prices are
valid for that row.

## Permissions

- View the planning page/API: any role with Spare Parts section access
  (same as the existing Dashboard/History screens) — `GET
  /api/spare-parts/planning`, `GET /api/spare-parts/planning/export.<fmt>`.
- Mark a part Critical: **Super Administrator only** (existing
  `PATCH /api/spare-parts/<id>` endpoint, gated by
  `_critical_forbidden_response()`). Manager may still edit this same
  spare part's other fields; a mixed request containing `critical`/
  `critical_note` is rejected in full.
- Pricing/cost figures (unit prices, projected procurement cost, savings,
  cost-based summary cards): **Super Administrator only** — identical to
  every other financial figure in Spare Parts (`_may_see_pricing()`); even
  Manager does not see these.

## Exports

CSV / XLSX / PDF via the existing `export_service.build_export()` — the
PDF export doubles as the "printable procurement report" (title, filters,
generated date, and the assumptions above are always included).

## Known limitations

- Confirmed incoming stock is always "Not tracked" (no purchase-order
  tracking yet) — never shown as a bare 0, which would wrongly imply a
  verified absence of pending orders. Procurement reports/exports state
  this explicitly in their assumptions text.
- "Department" and "machine" filters mean "ever issued to/associated with
  that department/machine", not "every machine this SKU could physically
  fit" in the department case.
- Custom date-range analysis anchors the recent/preceding trend halves at
  the custom range's end date, not necessarily "today".
