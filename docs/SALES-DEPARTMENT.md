# Sales Department

## Purpose and boundaries

The Sales Department is a section of Daily Figures for field-sales vehicle stock, priced sales, receivables and salesperson money accountability. It reuses `User`, `Product`, historical `PackagingRule`, `Customer`, `SalesCategory`, and finalized `Dispatch` records. It is not a second product catalog, customer database, authentication system, Finished Goods stock ledger, or warehouse Returns book.

`Closing Vehicle Stock` means the physical stock counted in the salesperson's vehicle at the end of a selling day. It is an observation and does **not** post a Finished Goods warehouse return. If goods physically return to the factory, the existing Returns workflow remains authoritative.

## Access architecture

The new department and section both use code `sales`:

- Department: Sales Department
- Section: Sales

Section access answers where a user may enter; the existing global role answers what the user may do. A normal salesperson is an `Operator` explicitly granted the Sales section and linked one-to-one to a `SalesProfile`. Existing ordinary users receive no automatic Sales grant. `Super Admin` remains global by role, without a username exception.

Role behavior:

- Super Admin: all profiles, profile setup, baselines, corrections, reports and exports.
- Manager: all personnel, baselines, reopening/corrections and manual stock/cash adjustments.
- Accountant: all financial and receivable reads, payment allocation and exports; no operational stock authority.
- Operator + SalesProfile: own Sales Days, stock counts, invoices, payments, expenses, transfers and history only.
- Viewer: read-only across permitted Sales data.

Every `/api/sales/*` route is centrally gated to the Sales section. Operator profile ownership is derived server-side from the authenticated user; query strings, JSON profile IDs and URLs cannot expand it. Sales access does not grant Finished Goods APIs.

## Data model

The additive migration introduces:

- `sales_profiles`
- `sales_baselines`
- `sales_opening_stock_lines`
- `sales_days`
- `sales_vehicle_closing_lines`
- `sales_stock_adjustments`
- `sales_invoices`
- `sales_invoice_lines`
- `sales_payments`
- `sales_expenses`
- `sales_money_transfers`
- `sales_cash_adjustments`

Accounting records use soft voids/reversals, dated business events, actor IDs and audit entries. Critical create endpoints use unique client idempotency keys. Money is stored as whole UGX integers. Stock is stored in integer base units with the historical PackagingRule reference.

## Sales Profile and Dispatch

`SalesProfile` separates login identity from commercial salesperson identity. It stores display name, optional SalesCategory, optional vehicle/route label, and the existing salesperson-category `Customer` used as a Dispatch recipient.

New stock loaded on date `D` is derived live from finalized Dispatch lines whose recipient matches the profile and whose Dispatch date is `D`. Voiding or correcting a Dispatch changes Sales calculations automatically. Dispatch quantities are never copied to an authoritative Sales load table, so they cannot be double-counted. Manager/Super Admin manual stock adjustments are audited exceptions with a required reason.

## Vehicle stock calculation

For salesperson `S`, product `P`, date `D`:

```text
Opening Vehicle Stock = most recent prior physical Closing Vehicle Stock
                        OR explicit reconciliation opening stock

New Stock Loaded = finalized Dispatch base units
                   + active manual stock adjustments

Available Stock = Opening Vehicle Stock + New Stock Loaded

Stock-derived Sold = Available Stock - Closing Vehicle Stock

Quantity Variance = Stock-derived Sold - Recorded/priced Sale base units
```

No historical evidence means `Opening balance required`; zero is never silently assumed. Closing stock greater than available is rejected, not clamped. A normal close requires an explicit closing count for each active product and exact quantity reconciliation. An elevated override requires a reason and remains visibly flagged.

Changing an old closing count immediately changes that date's derived sold quantity and the next selling day's opening. It never edits invoice quantities, prices, customers or payments. Later physical counts remain independent observations and become the next anchor. The correction-preview endpoint shows the immediate derived, variance and next-opening impact before an elevated correction.

## Priced sales and receivables

An invoice has one or more server-calculated lines. Each line contains one product, one selling unit, a whole quantity and a unit price. The same product may appear repeatedly at different prices; lines are never merged. Carton, pack and piece quantities convert through the PackagingRule effective on the business date, and the rule ID/base quantity are stored on the line.

```text
Line Total = whole selling-unit quantity × whole UGX unit price
Invoice Total = sum of server-calculated line totals
Current Outstanding = Invoice Total - all active allocated payments
Outstanding as of date D = Invoice Total - active payments received on/before D
```

Any transaction that leaves an outstanding balance requires a real Customer. Overpayment is rejected. Opening receivables are special customer-linked invoices that establish historical debt but are excluded from Total Sales.

Payments are independent dated ledger entries. A late payment reduces current debt and contributes to cash/mobile receipts only on its actual payment date. It does not rewrite the original sale date or historical day-close receivable. Debtor aging uses the application business date and buckets 0–30, 31–60, 61–90 and 90+ days.

## Money accountability

Accounts distinguish Cash, Mobile Money and external destinations such as Company/Bank. Expenses carry their actual payment method. Only cash expenses reduce physical cash; Mobile Money expenses reduce Mobile Money. Transfers move value between accounts and never alter Total Sales, Expenses or Net Sales.

```text
Net Sales After Expenses = Total Sales - all field expenses

Expected Cash Closing = trusted opening cash
                        + cash payments received (including old debts)
                        + transfers into cash
                        - cash expenses
                        - transfers/remittances out of cash
                        +/- explicit audited cash adjustments
```

Mobile Money uses the same ledger pattern with its own opening balance, receipts, expenses, transfers and adjustments.

Actual Cash Count and optional Actual Mobile Money Balance are physical observations:

```text
Cash Variance = Actual Cash Counted - Expected Cash Closing
```

A negative value is a shortage and a positive value is an overage. Actual counts never reset or overwrite the expected ledger. Only a Manager/Super Admin explicit adjustment with a reason changes expected balances. Consequently an unresolved shortage remains detectable on later dates.

## Reconciliation and first divergence

The reconciliation timeline shows Sales, receipts by method, old-debt collections, new receivables, expenses, net sales, expected/actual cash, cash variance, stock variance and day status. `First Unresolved Variance` scans the selected profile/range in business-date order and returns the first physical cash mismatch or stock-to-priced-sales mismatch. It reports evidence, not a guessed cause.

Reports are available as CSV, XLSX and branded PDF for daily summaries and date-range reconciliation. Operator exports are restricted to the authenticated profile; authorized elevated roles may select all or one profile. The Sales page also exposes four focused exports, each audited as an `export` action:

- `debtors` (as of a date): outstanding receivables with aging buckets
- `payments` (date range): every active receipt by date, salesperson, method and reference
- `expenses` (date range): active field expenses with their paying account
- `stock` (date range): opening, loaded, adjustments, closing, stock-derived sold, priced sold and variance per product/day

Every report and export page links to `csv`, `xlsx` and `pdf` for the currently selected salesperson and date, so nobody has to hand-write query strings.

The Sales screen itself is a single mobile-first page: a daily dashboard card (receivable total, unresolved days), stock rows with closing-count entry, invoice lines with live line totals, expense entry with previously-used description suggestions, a receipts list, transfer and adjustment forms, a reopen button for closed days, and Manager/Super Admin settings for baselines, opening receivables, profile setup, stock adjustments and cash adjustments.

## Corrections, idempotency and audit

Closed days require Manager/Super Admin reopening. Historical stock-count corrections and all manual stock/cash adjustments require reasons. Invoices, payments, expenses, transfers and stock adjustments are soft-voided with actor, timestamp and reason. Audit records capture before/after values without secrets. Unique idempotency keys prevent duplicate sale, payment, expense and transfer creation after double-clicks or network retries.

The server validates dates, whole nonnegative values, account/method codes, packaging units, customer-credit identity, ownership, available stock and outstanding balances. Browser totals are display previews only; Python services are authoritative.

## Migration and deployment notes

Revision `de22eee9674b` follows `a6c8e4922fbc`. It creates only new Sales tables and seeds the new department/section. It does not alter existing Products, Customers, Users or grants and does not grant Sales to ordinary users. Upgrade and downgrade are supported, leaving one Alembic head.

Deployment is intentionally outside this feature task. Review and merge the feature branch separately, back up the production database using existing procedures, then run the normal migration/startup process. Never map Closing Vehicle Stock into warehouse Returns during deployment or data import.

## Verification

Server-side behaviour is covered by `tests/test_sales.py` (daily totals, stock reconciliation, receivables, salesperson scoping/IDOR, cash shortages that must not reset the expected ledger, and the four focused exports including their audit rows):

```bash
python -m pytest tests/test_sales.py -q
```

The mobile acceptance flow runs against a disposable database with Playwright (viewport 375×812), seeded by `scripts/_sales_browser_seed.py`:

```bash
# terminal 1 — disposable data, never production
export DB_PATH=/tmp/sales-browser.db SECRET_KEY=local-test
python -m flask db upgrade
python scripts/_sales_browser_seed.py
python -m flask run --port 5055

# terminal 2
SALES_TEST_BASE_URL=http://127.0.0.1:5055 npx playwright test
```

The browser test signs in as an Operator, loads the day, counts every product, records a sale and an expense, closes the day, confirms the ownership check returns 403 for a foreign profile ID and asserts the page has no horizontal overflow at 375px.

