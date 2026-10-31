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
- `sales_debtors` (free-text debtor names, per SalesProfile)
- `sales_debts` (unpaid amounts; `sale` or `opening`)
- `sales_debt_payments` (collections dated when received)

Accounting records use soft voids/reversals, dated business events, actor IDs and audit entries. Critical create endpoints use unique client idempotency keys. Money is stored as whole UGX integers. Stock is stored in integer base units with the historical PackagingRule reference.

## Sales Profile and Dispatch

`SalesProfile` separates login identity from commercial salesperson identity. It stores display name, optional SalesCategory, optional vehicle/route label, and the existing salesperson-category `Customer` used as a Dispatch recipient.

New stock loaded on date `D` is derived live from finalized Dispatch lines whose recipient matches the profile and whose Dispatch date is `D`. Voiding or correcting a Dispatch changes Sales calculations automatically. Dispatch quantities are never copied to an authoritative Sales load table, so they cannot be double-counted. Manager/Super Admin manual stock adjustments are audited exceptions with a required reason.

## Vehicle stock and Returns (the core rule)

A "return" in this module is unsold stock the driver brought back and left in
the vehicle. It is never a warehouse/customer return, refund or damaged-stock
transaction, and a vehicle return never posts into Finished Goods. The next
morning the stock given is added to whatever the vehicle still holds:

```text
Previous Return  = most recent prior closing Return (or the trusted opening)
Stock Added      = finalized Dispatch base units + active stock adjustments
Total Stock      = Previous Return + Stock Added
Return Today     = the physical count still in the vehicle at day end
Sold             = Total Stock − Return Today
Difference       = Sold (stock) − quantities entered on the sale lines
```

For salesperson `S`, product `P`, date `D` the same rule is stored/derived as:

```text
Opening Vehicle Stock = most recent prior physical Closing Vehicle Stock
                        OR explicit reconciliation opening stock

New Stock Loaded = finalized Dispatch base units
                   + active manual stock adjustments

Available Stock = Opening Vehicle Stock + New Stock Loaded

Stock-derived Sold = Available Stock - Closing Vehicle Stock

Quantity Variance = Stock-derived Sold - Recorded/priced Sale base units
```

A profile with no trusted starting point at all shows `Starting point
required`; zero is never silently assumed for a vehicle that has never been
counted. Once a trusted starting point exists for that vehicle, a product it
does not list means "not carried yet", so newly dispatched stock reconciles
from zero instead of blocking the day. Closing stock greater than total stock
is rejected, not clamped. A normal close requires an explicit closing count for
each product and exact quantity reconciliation. An elevated override requires a
reason and remains visibly flagged.

Changing an old closing count immediately changes that date's sold quantity and
the next selling day's opening return. It never edits invoice quantities,
prices, debtors or payments. Later physical counts remain independent
observations and become the next anchor. The correction-preview endpoint shows
the immediate derived, difference and next-opening impact before an elevated
correction.

## Priced sales and receivables

An invoice has one or more server-calculated lines. Each line contains one product, one selling unit, a whole quantity and a unit price. The same product may appear repeatedly at different prices; lines are never merged. Carton, pack and piece quantities convert through the PackagingRule effective on the business date, and the rule ID/base quantity are stored on the line.

```text
Line Total = whole selling-unit quantity × whole UGX unit price
Invoice Total = sum of server-calculated line totals
Current Outstanding = Invoice Total - all active allocated payments
Outstanding as of date D = Invoice Total - active payments received on/before D
```

An ordinary paid sale needs no customer at all. The legacy `customer_id` /
per-sale payment columns stay on historical rows and remain readable: when a
sale does carry a customer and is paid only in part, that remainder is a
customer receivable (recorded through the original per-sale credit path, which
still requires a real Customer and rejects overpayment). New unpaid amounts use
the free-text Debtors ledger instead (below). Opening receivables are
customer-linked historical invoices that never count as current Total Sales.

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

The Sales screen itself is a single mobile-first page. For a driver it is five steps: stock & returns (the paper equation, one prominent Return Today entry per carried product and one Save Returns action), sales value grouped under each sold product with its price lines and TOTAL SALES, debtors / Mobile Money / expenses with TOTAL EXPENSES and NET SALES AFTER EXPENSES, a vertical balance from Total Sales to Cash Expected, Cash Counted and Difference, and Close day. Managers and Super Administrators keep the salesperson selector, historical dates, reopening, corrections and the Settings workspace for baselines, opening receivables, profile setup, stock adjustments and cash adjustments.

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
export DB_PATH=/tmp/sales-browser.db SECRET_KEY=local-test PYTHONPATH=.
python -m flask db upgrade
python scripts/_sales_browser_seed.py
python -m flask run --port 5055

# terminal 2
SALES_TEST_BASE_URL=http://127.0.0.1:5055 npx playwright test
```

The browser test signs in as a normal Sales Operator and asserts the driver's
first-load experience: no salesperson selector, today selected and opened
automatically, Return Today inputs visible without pressing anything, stock
figures read-only, no customer or Walk-in field, no horizontal overflow, and no
product that carries nothing. It then switches to 30/10/2026 (shown, not
silently created, until "Start this date" is tapped) and reproduces the paper
sheet: Compact 50 + 100 = 150 − 40 = 110 sold and Napkins 10 + 20 = 30 − 5 = 25
sold, one Save Returns action for both, four price lines (100 × 79,000;
10 × 80,000; 20 × 103,000; 5 × 104,000) giving TOTAL SALES 11,280,000, three
cash expenses giving TOTAL EXPENSES 40,000, NET SALES AFTER EXPENSES and CASH
EXPECTED 11,240,000, counted cash leaving a zero difference, then Close day.
Finally it confirms the ownership check returns 403 for a foreign profile ID and
that every visible control is touch-sized at 375×812.

## Operator workflow (corrected)

The person using the Daily Sales screen is a driver / field salesperson, not an
accountant. The screen follows the real-world order of the day and mirrors the
handwritten sheet:

1. Date. A driver has no salesperson selector and no "Open day" step: today is
   selected and opened automatically and the stock rows render immediately,
   because opening a Sales Day is not a financial transaction. Choosing another
   date loads it; a date with no day shows a single "Start this date" action, so
   scrolling the calendar never creates empty historical days.
2. Stock & returns. Each carried product is shown as the paper equation:
   Previous Return, Stock Added and Total Stock (read-only), a prominent
   RETURN TODAY entry, and Sold, which previews as soon as the driver types.
   One Save Returns button sends every changed row. Previous Return is filled
   from the last counted return, so nothing is retyped in the morning.
3. Sales value. Price entries are grouped beneath the product the stock already
   proved was sold ("Sold from stock: 110 cartons"), each line being
   quantity × price, with "Add price" for another selling price of the same
   product. Every group shows Sold from stock, Priced and the difference
   ("✓ Balanced" or "5 cartons still need a selling price"). TOTAL SALES is one
   prominent figure.
4. Debtors, Mobile Money and expenses. Debtor names stay free text and are
   remembered; Mobile Money is one amount for the day; expenses are
   description, amount and paid-from, followed by TOTAL EXPENSES and NET SALES
   AFTER EXPENSES. Money transfers/handover stay collapsed.
5. Balance. Total Sales, less Debtors, less Mobile Money, cash from today's
   sales, plus old debts collected, less cash expenses, less cash handed over,
   plus cash brought forward, then CASH EXPECTED, Cash Counted and Difference.
   Mobile Money has its own expected/counted/difference.
6. Close day.

An ordinary paid sale never asks for a customer, invoice, per-sale payment
method or amount received. The same product can be sold at several prices on
the same day and every price line stays its own sale line. Unpaid amounts are
recorded separately as a name and an amount.

Browser arithmetic is a preview only. Every saved return, sales price, expense
and count is recalculated and validated by the Python services, and a Manager
still needs a recorded reason to change an already-saved return.

The daily stock list only shows products that belong to that salesperson's
vehicle balance: a positive opening count, stock loaded today, a recorded
adjustment, a sale, or an existing closing count. A driver is never forced to
type zero for every unrelated active product. Advanced stock adjustments stay
Manager/Super Admin controls. If the physical count and the entered sales
disagree, a plain warning says how many units differ; the correction never
edits the sales, prices, debts, cash or Mobile Money.

### Debtors are free text

`sales_debtors`, `sales_debts` and `sales_debt_payments` (migration
`19f4c7a2b8d1`) store the salesperson's own debtor names, scoped to their
SalesProfile. Names are normalized (whitespace collapsed, case-folded), so
"Darius", "darius" and "DARIUS" are one debtor and one suggestion. Names stay
available after a debt is settled and never create rows in the Finished Goods
`customers` table. `kind='sale'` reduces that day's collectable cash;
`kind='opening'` is historical debt established by a Manager and never counts
as current-period sales. Payments are allocated oldest-first internally, but
the operator only sees "outstanding" and "record payment".

### Tender split and expected money

`sales_days.mobile_money_from_sales_ugx` records the Mobile Money collected
from that day's sales. When it is NULL, legacy per-sale Mobile Money receipts
for the same date are used, so deployed history reconciles exactly as before.

```text
Cash from today's sales = Total Sales − New debts − Mobile Money from sales

Expected Cash = Opening Cash
              + Cash from each day's sales
              + Old debts collected in Cash (on the receipt date)
              + Transfers into Cash
              − Cash expenses
              − Transfers out of Cash
              ± audited Cash adjustments
```

Mobile Money follows the same shape. A debt is not a second sale: recording
it leaves Total Sales unchanged and only reduces that day's collectable cash.
A later payment increases Cash or Mobile Money on the date received, reduces
the debtor's outstanding balance, and never rewrites the original sale date.
New debts plus Mobile Money may never exceed the sales already entered; the
API rejects that impossible combination.

### Day close

Closing requires a physical Cash Counted figure. Expected cash stays
ledger-derived, so a shortage or overage appears as the Difference and is
never written back as the expected balance. Mobile Money has its own count and
Difference, so cash counted can never hide a Mobile Money gap. Only an audited
Manager/Super Admin adjustment changes the expected books.

### Role-based view

Operators see Daily and Debtors. Reconciliation and exports remain available
to Managers, Accountants, Viewers and Super Admins. Hiding a tab is never used
as authorization: every route keeps its existing server-side role and
ownership checks, forged profile IDs are rejected, and the operator profile is
always derived from the authenticated user.

