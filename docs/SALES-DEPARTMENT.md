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
Previous Return  = most recent prior counted Return (or the trusted opening)
Given Stock      = finalized Dispatch base units + active stock adjustments
Total Stock      = Previous Return + Given Stock
Return Today     = the physical count still in the vehicle at day end
Sold Today       = Total Stock − Return Today
Difference       = Sold Today (stock) − quantities entered on the sale lines
```

All four stock figures are the salesperson's own working numbers and every one
of them is editable on the day being worked on: Previous Return, Given Stock,
Total Stock and Return Today. A correction is written to that day's own row, so
correcting today's Previous Return never changes yesterday's counted return.
Total Stock is prefilled from Previous Return + Given Stock and stops following
the formula as soon as the salesperson corrects it by hand. A figure that still
equals its system value is not copied into the day's row, so a later dispatch or
a corrected earlier day still carries forward until the salesperson corrects it
themselves.

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

A product with no counted return and no trusted opening shows a blank Previous
Return and the card asks for the starting figure; zero is never silently assumed
for a vehicle that has never been counted. Once a trusted starting point exists
for that vehicle, a product it does not list means "not carried yet", so newly
dispatched stock reconciles from zero instead of blocking the day. Return Today
greater than Total Stock is rejected, not clamped. A normal close requires a
counted return for each product on the vehicle and exact quantity
reconciliation. An elevated override requires a reason and remains visibly
flagged.

Changing an old closing count immediately changes that date's sold quantity and
the next selling day's opening return. It never edits invoice quantities,
prices, debtors or payments. Later physical counts remain independent
observations and become the next anchor. The correction-preview endpoint shows
the immediate derived, difference and next-opening impact before an elevated
correction.

## Priced sales and receivables

An invoice has one or more server-calculated lines. Each line contains one product, one selling unit, a quantity and a unit price. The same product may appear repeatedly at different prices; lines are never merged. Carton, pack and piece quantities convert through the PackagingRule effective on the business date, and the rule ID/base quantity are stored on the line. A sales card quantity may be fractional (`0.5`, `0.25`, `1.5`), because a salesperson sometimes knows a part of a carton as a known equivalent amount; quantities are held as exact three-decimal values and every comparison against Sold Today is done on those exact values, never on binary floats.

```text
Line Total = selling-unit quantity (whole or decimal) × whole UGX unit price, rounded half-up to whole UGX
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

The Sales screen itself is a single mobile-first page with six numbered steps: vehicle stock (one editable card per product the salesperson put on the vehicle — Product, Previous Return, Given Stock / Issued Today, Total Stock, Return Today and a calculated Sold Today, with Add Product), one sales card per product that actually sold, total sales today, Mobile Money once for the whole day, expenses and money movement, and cash at hand ending in Close day. Sold Today is calculated once, inside its Vehicle stock card, and the same figure drives that product's sales card and the page totals as the salesperson types; there is no separate Sold today section and no separate Vehicle stock save.

**Save everything once, at Close day.** Until Close day both the Vehicle stock sheet and every Sales Card exist only in the browser: a keystroke writes nothing, there is no autosave, no stock-only save button and no per-product Save Sales button. Sales Cards are live — typing a quantity or a price repaints that product's card total, Total sales today and the money that depends on them immediately. A Sales Card has no unit selector of its own because its quantity is always a **carton-equivalent**: `1` is one carton, `0.5` half a carton, `0.25` a quarter and `1.5` one and a half, and the row label always reads cartons. Pieces and packs are only ever derived from the product's PackagingRule (`0.5` cartons of a 10-pack carton is 5 packs, of a 12-pack carton 6 packs), so the Vehicle stock "Counted in" selector — which may be Cartons, Packs or Pieces for the counts on the sheet — can never change what a number on a Sales Card means. A card added with "Add Another Product" can be cancelled with its own compact ×, which removes the extra browser card, frees the product in the dropdown again and writes nothing at all; products already on the vehicle carry no × and can never be deleted from the screen. Close day (`POST /api/sales/days/<date>/close`) is the one write: it validates the submitted sheet, stores it through `save_vehicle_stock()`, derives Sold Today from the figures it just stored, stores the complete Sales Cards it was handed (`sales_cards`, through `save_product_sales()`), re-checks every card against those figures, validates the cash/money side and closes the day — all in a single transaction. If any check fails nothing from that attempt is committed (no partial sheet, no partial card, no partial close), the day stays open and the typed figures stay on screen to correct. The per-card `PUT /api/sales/days/<date>/products/<id>/sales` endpoint still exists for API clients, but the Sales screen no longer uses it.

A salesperson completes all six alone: no Manager or Super Administrator opens the day, approves a starting count or countersigns the return. Managers and Super Administrators keep the salesperson selector, historical dates, reopening, corrections and the Settings workspace for baselines, opening receivables, profile setup, stock adjustments and cash adjustments.

**Warnings describe the screen.** The stock-vs-sales warning ("stock count says 1 carton sold, cards price 5 packs (difference 5 packs)") is worked out on every repaint from the figures in the Vehicle Stock boxes and the price rows on screen, so it can never quote a saved day's figures while a different count is being typed; a closed day, where nothing is being typed, shows the warning stored when it was closed.

## Resetting Sales transactional data

Testing the workflow leaves Sales rows that afterwards read like real accounting history. `scripts/reset_sales_data.py` removes that transaction layer and nothing else:

```bash
# read-only: prints exactly what would be removed and changes nothing
python scripts/reset_sales_data.py --database data/production.db

# take a verified backup, then reset
python scripts/reset_sales_data.py --database data/production.db \
    --apply --confirm RESET-SALES-DATA
```

It clears `sales_debt_payments`, `sales_debts`, `sales_debtors`, `sales_payments`, `sales_invoice_lines`, `sales_invoices`, `sales_expenses`, `sales_money_transfers`, `sales_cash_adjustments`, `sales_stock_adjustments`, `sales_vehicle_closing_lines`, `sales_days`, `sales_opening_stock_lines` and `sales_baselines` — child rows first, in one transaction that is rolled back on any error.

It never touches products, packaging rules, sales categories, customers, users, sales profiles, sections and permissions, feature flags, company settings, the audit trail, any other department's rows, the database schema or the Alembic history, and it never drops or recreates a table. Finished Goods dispatches are not Sales data: they are the evidence that stock reached the vehicle, so a load already made still appears as Given Stock after a reset, with no counted return and no sold figure of its own.

`--apply` refuses to run without `--confirm RESET-SALES-DATA`, and before deleting anything it writes a transactionally consistent `<db>.bak-<timestamp>` next to the database and verifies it with `PRAGMA integrity_check`. The command prints the rows removed, the master data that stayed, and re-reads the database to prove the Sales tables are empty. `tests/test_sales_reset.py` covers the zero state, the preserved master data, the clean day that follows, and the CLI's confirmation and backup behaviour.

## Corrections, idempotency and audit

Closed days require Manager/Super Admin reopening, with the reason recorded. Correcting the four Vehicle Stock figures on an open day (Previous Return, Given Stock, Total Stock, Return Today) is the salesperson's own ordinary work and asks for no approval; a reason supplied with a correction is still stored and audited. Manual stock and cash adjustments require reasons. Invoices, payments, expenses, transfers and stock adjustments are soft-voided with actor, timestamp and reason. The compact × on an expense cancels it without a prompt and without reloading the page: the row is soft-voided with the recorded reason "Removed from the Sales screen", it leaves the active list, the expense totals and Cash at Hand follow, and the browser-only Vehicle stock, Sales Card, Mobile Money and counted-cash values on screen are left exactly as typed. Expense descriptions are remembered from the expenses already recorded (newest first, de-duplicated case-insensitively) so the form can suggest them while typing; no separate table is needed, and a description stays remembered even after its expense was cancelled. Audit records capture before/after values without secrets. Unique idempotency keys prevent duplicate sale, payment, expense and transfer creation after double-clicks or network retries.

The server validates dates, whole nonnegative values, account/method codes, packaging units, customer-credit identity, ownership, available stock and outstanding balances. Browser totals are display previews only; Python services are authoritative.

## Migration and deployment notes

Revision `de22eee9674b` follows `a6c8e4922fbc`. It creates only new Sales tables and seeds the new department/section. It does not alter existing Products, Customers, Users or grants and does not grant Sales to ordinary users. Upgrade and downgrade are supported, leaving one Alembic head.

Revision `a3d9f1c7b2e4` follows `19f4c7a2b8d1` and adds the editable Vehicle Stock figures to `sales_vehicle_closing_lines`: `previous_return_base_qty`, `issued_base_qty`, `total_stock_base_qty` (nullable — NULL means "follow the system figure") and `return_counted` (backfilled to true for every existing row). It is additive: no existing quantity is changed and no historical row is rewritten.

Revision `c4f1a7d90b23` follows `a3d9f1c7b2e4` and widens the two quantity columns of `sales_invoice_lines` (`quantity`, `base_unit_qty`) from `Integer` to `Numeric(12, 3)`, so a Sales Card can price a fraction of a unit. It is a widening type change only: every existing value is a whole number and keeps it, money columns stay whole UGX integers, and no Vehicle Stock quantity changes. The CHECK constraints on the table are re-stated in the same migration because a SQLite batch rebuild cannot carry them across. Expense description suggestions need no table: they are read from `sales_expenses.description` (newest first, de-duplicated case-insensitively), so no new schema was added for them.

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

The browser test signs in as a normal Sales Operator and asserts that today's Sales Day opens
itself with no salesperson selector. It then opens 30/10/2026 and asserts the six steps in
the order a salesperson works them, reproducing the paper sheet: Compact 50 carried + 100
delivered by Finished Goods = 150, return 40 → 110 sold; Napkins 10 + 20 = 30, return 5 →
25 sold. Sold Today is calculated live inside its Vehicle stock card and repaints that
product's sales card while the return is typed, with no save and no reload. An extra card
added with "Add Another Product" appears with its own ×, disappears when × is pressed, frees
its product in the dropdown again (a catalogue-only product such as Straws can be picked,
cancelled and picked again), and never touches the database. Each product then gets one live
card with price rows — Compact 1.5 × 79,000 + 1.5 × 80,000 = UGX 238,500 for 3 cartons,
Napkins 20 × 103,000 + 5 × 104,000 = UGX 2,580,000, Straws 0.5 + 0.5 × 5,000 — with no Save
Sales button on any card and no Unit selector on any row: the row label always reads cartons,
because a card quantity is always a carton-equivalent (0.5 / 0.25 / 1.5 are accepted and price
the product's PackagingRule, whatever unit Vehicle stock is counted in), and an over-priced
fractional card is refused with the exact quantity to change. Total Sales is UGX 2,823,500
while nothing is stored yet. Three expenses are recorded, then the sheet is changed again and
the middle expense is cancelled with its ×: only that expense leaves the list, Cash at Hand
follows it, and the Vehicle stock figures, the Sales Card rows, the added Straws card and the
typed Mobile Money/counted cash stay exactly as entered with no Vehicle stock request at all.
A refused close (Napkins priced for 25 but 20 counted) writes nothing and leaves every typed
figure on screen; the corrected close stores the sheet and all three cards in one request,
reloading reproduces it exactly, and a closed day is read-only. It also proves the ownership
check returns 403 for a foreign profile ID, that there is no horizontal overflow, that every
control — the × buttons and the description suggestions included — is at least 36px tall at
375×812, that "Lunch" recorded on 30/10 is offered as a tappable suggestion on 31/10 while
"lunch" does not become a second suggestion, and that yesterday's closed record stays
untouched.

## Operator workflow (corrected)

The person using the Daily Sales screen is a salesperson, not an accountant. The screen is
six numbered steps that follow the real-world order of the day, and one person can complete
all six without a Manager or Super Administrator watching:

1. Vehicle stock. The salesperson chooses the products that are physically on the
   vehicle today with Add Product, which lists the live product catalogue
   (`GET /api/sales/days/<date>/vehicle-stock/options`) and never a list written in
   JavaScript: a product added to the catalogue later is selectable with no code
   change, and a product already on the sheet is not offered twice. Each card shows
   Product, Previous Return, Given Stock / Issued Today, Total Stock, Return Today
   and Sold Today. Previous Return and Given Stock are prefilled from the last
   counted return and today's finalized dispatch; Total Stock is prefilled from
   Previous Return + Given Stock and Sold Today is Total Stock − Return Today,
   recalculated live. All four stock figures stay editable — the salesperson is
   reconciling their own vehicle, so no Manager, unlock or approval is involved. A
   return greater than total stock is refused with a plain-language warning.
2. Sales cards. One card per product. The card already knows the counted quantity
   ("133 cartons sold today, from Vehicle stock"), so the salesperson just adds one or more
   price rows and the card says whether they add up. Everything is live — there is no Save
   Sales button: a row that under- or over-prices the counted quantity is called out with the
   exact quantity that has to change ("... but only 10 cartons were sold") as the numbers are
   typed, quantities may be fractional carton-equivalents, and switching the Vehicle stock
   "Counted in" selector never changes what a card number means, so the card needs no Unit
   selector. Close day stores every card, so a product's
   lines are replaced in one transaction at the close instead of being saved per product.
3. Total sales today. One prominent figure for the day; it is the live sum of the cards on
   screen, so a changed quantity or price moves it immediately and the close never counts the
   same sale twice.
4. Mobile Money. One amount for the whole day, never one per row.
5. Expenses and money movement. Description, amount and paid-from, plus money
   transfers/handover, which stay collapsed until they are needed. Descriptions already used
   are offered as tappable suggestions while typing, and the compact × on a row cancels that
   expense immediately — the totals follow and nothing else on the page is reloaded.
6. Cash at hand. Opening cash (a declared baseline, or nothing from the salesperson's first
   Sales Day when no baseline has been declared), plus cash received on today's sales, plus
   old debts collected in cash, plus money transferred in, less cash expenses, less money
   handed over, plus audited cash adjustments gives Cash Expected; then Cash Counted and
   Difference. Mobile Money has its own expected/counted/difference. Because the books fall
   back to the salesperson's own activity, nobody needs a Manager to unlock the ledger before
   counting. Then Close day, which saves everything at once: the whole Vehicle stock sheet,
   the day's returns, the sales cards' quantities and the close itself in one transaction.

An ordinary paid sale never asks for a customer, invoice, per-sale payment method or amount
received. The same product can be sold at several prices on the same day and every price line
stays its own sale line. Unpaid amounts are recorded separately as a name and an amount.

Browser arithmetic is a preview only. Every saved return, sales price, expense and count is
recalculated and validated by the Python services. A salesperson may void their own mistake
while the day is still open; a closed Sales Day must be reopened by a Manager or Super
Administrator before anything in it can change.

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

