const { test, expect } = require('@playwright/test');

const BUSINESS_DATE = '2026-10-30';
const NEXT_DATE = '2026-10-31';

const stockRow = (page, name) => page.locator('.vstock-row').filter({ hasText: name });
const salesCard = (page, name) => page.locator('.sales-card').filter({ hasText: name });

async function loginAsSalesOperator(page) {
  await page.goto('/');
  await page.locator('[data-section="sales"]').click();
  await page.locator('#usernameInput').fill('browser-sales');
  await page.locator('#passwordInput').fill('BrowserPass123');
  await page.locator('#loginBtn').click();
  await expect(page).toHaveURL(/\/sales\.html/);
  await expect(page.getByRole('heading', { name: 'Daily accountability' })).toBeVisible();
  await expect(page.locator('#statusPill')).not.toHaveText('Not loaded');
  // The page loads its own default day on boot; wait for that to settle so a
  // later repaint can never overwrite figures typed by the test.
  await page.waitForLoadState('networkidle');
}

async function openDay(page, date) {
  // Changing the date is what loads it (the page listens for the change), so
  // this waits for that one request instead of firing a second load on top.
  const response = page.waitForResponse(r => r.url().includes(`/api/sales/days/${date}?`)
    && r.request().method() === 'GET');
  await page.locator('#businessDate').fill(date);
  await response;
  await page.waitForLoadState('networkidle');
  await expect(page.locator('#stockList')).not.toContainText('Open a day to begin.');
}

async function chooseDebtor(page, name) {
  const value = await page.locator('#collectionDebtor option', { hasText: name })
    .first().getAttribute('value');
  await page.locator('#collectionDebtor').selectOption(value);
}

async function dayJson(page, date) {
  return page.evaluate(async (d) => (await (await fetch(`/api/sales/days/${d}`)).json()), date);
}

async function stockOf(page, date, productName) {
  const day = await dayJson(page, date);
  return (day.stock || []).find((row) => row.product_name === productName);
}

async function sheetTotal(page, name) {
  return Number(await stockRow(page, name).locator('[data-stock-field="total"]').inputValue());
}

// Count every product back in full: nothing sold, nothing to price.
async function countEverythingBack(page) {
  const rows = page.locator('.vstock-row');
  const total = await rows.count();
  for (let index = 0; index < total; index += 1) {
    const row = rows.nth(index);
    const figure = Number((await row.locator('[data-stock-field="total"]').inputValue()) || 0);
    await row.locator('[data-stock-field="returned"]').fill(String(figure));
  }
  // Prove every card really holds a counted figure before Close day is pressed.
  for (let index = 0; index < total; index += 1) {
    await expect(rows.nth(index).locator('[data-stock-field="returned"]')).not.toHaveValue('');
  }
}

async function closeDay(page) {
  const waiter = page
    .waitForResponse(r => r.url().endsWith('/close') && r.request().method() === 'POST', { timeout: 5000 })
    .catch(() => null);
  await page.locator('#closeDay').click();
  const response = await waiter;
  if (!response) {
    throw new Error(`Close day sent no request. On-screen message: "${await page.locator('#message').textContent()}"`);
  }
  return response;
}

async function saveWorkingDay(page) {
  const waiter = page
    .waitForResponse(r => r.url().endsWith('/save') && r.request().method() === 'POST', { timeout: 5000 })
    .catch(() => null);
  await page.locator('#saveDay').click();
  const response = await waiter;
  if (!response) {
    throw new Error(`Save sent no request. On-screen message: "${await page.locator('#message').textContent()}"`);
  }
  return response;
}

async function showForm(page, id) {
  const form = page.locator('#' + id);
  if (!(await form.isVisible())) await page.locator(`[data-toggle-form="${id}"]`).click();
  await expect(form).toBeVisible();
}

// Enter a whole day's working values without closing it.
async function typeAWorkingDay(page) {
  await stockRow(page, 'Compact Standard').locator('[data-stock-field="returned"]').fill('40');
  await stockRow(page, 'Napkins Standard').locator('[data-stock-field="returned"]').fill('5');

  const compact = salesCard(page, 'Compact Standard');
  await compact.locator('[data-card-field="quantity"]').first().fill('100');
  await compact.locator('[data-card-field="unit_price_ugx"]').first().fill('79000');

  await showForm(page, 'expenseForm');
  await page.locator('#expenseDescription').fill('Lunch');
  await page.locator('#expenseAmount').fill('10000');
  await page.locator('#addExpense').click();
  await expect(page.locator('#expenseList')).toContainText('Lunch');

  await page.locator('#mobileFromSales').fill('300000');
  await page.locator('#actualCash').fill('100000');

  await page.locator('#debtorName').fill('Darius');
  await page.locator('#debtAmount').fill('100000');
  await page.locator('#addDebt').click();
  await expect(page.locator('#debtList')).toContainText('Darius owes today');
}

test('A/B/C: an unfinished day is browser state and never comes back', async ({ page }) => {
  await loginAsSalesOperator(page);

  // Record every write the page attempts: before Close Day there must be none.
  const writes = [];
  page.on('request', (request) => {
    const method = request.method();
    if (method !== 'GET' && request.url().includes('/api/sales/')) {
      writes.push(`${method} ${request.url()}`);
    }
  });

  await openDay(page, BUSINESS_DATE);
  await typeAWorkingDay(page);
  expect(writes).toEqual([]);

  // Leaving the date and coming back gives a clean, unsaved day.
  await openDay(page, NEXT_DATE);
  await openDay(page, BUSINESS_DATE);
  await expect(page.locator('#expenseList')).not.toContainText('Lunch');
  await expect(page.locator('#debtList')).not.toContainText('Darius');
  await expect(page.locator('#mobileFromSales')).toHaveValue('');
  await expect(page.locator('#actualCash')).toHaveValue('');
  await expect(salesCard(page, 'Compact Standard').locator('[data-card-field="quantity"]').first())
    .toHaveValue('');
  await expect(stockRow(page, 'Compact Standard').locator('[data-stock-field="returned"]'))
    .toHaveValue('');

  // A refresh is the same story: nothing was stored, so nothing returns.
  await typeAWorkingDay(page);
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Daily accountability' })).toBeVisible();
  await openDay(page, BUSINESS_DATE);
  await expect(page.locator('#expenseList')).not.toContainText('Lunch');
  await expect(page.locator('#debtList')).not.toContainText('Darius');
  expect(writes).toEqual([]);

  // And the database agrees: no day, no stock, no cards, no money.
  const stored = await dayJson(page, BUSINESS_DATE);
  expect(stored.day).toBeNull();
  expect(stored.invoices).toEqual([]);
  expect(stored.expenses).toEqual([]);
  expect(stored.debts).toEqual([]);
  expect(stored.debt_payments).toEqual([]);
  expect(stored.totals.total_sales_ugx).toBe(0);
  const stock = await stockOf(page, BUSINESS_DATE, 'Compact Standard');
  expect(stock ? stock.closing : null).toBeNull();
});


test('D: Close day stores the whole day and reloading reproduces it', async ({ page }) => {
  await loginAsSalesOperator(page);
  await openDay(page, BUSINESS_DATE);
  await stockRow(page, 'Compact Standard').locator('[data-stock-field="returned"]').fill('40');
  await stockRow(page, 'Napkins Standard').locator('[data-stock-field="returned"]').fill('5');
  const compact = salesCard(page, 'Compact Standard');
  await compact.locator('[data-card-field="quantity"]').first().fill('110');
  await compact.locator('[data-card-field="unit_price_ugx"]').first().fill('79000');
  const napkin = salesCard(page, 'Napkins Standard');
  await napkin.locator('[data-card-field="quantity"]').first().fill('25');
  await napkin.locator('[data-card-field="unit_price_ugx"]').first().fill('100000');

  await showForm(page, 'expenseForm');
  await page.locator('#expenseDescription').fill('Lunch');
  await page.locator('#expenseAmount').fill('10000');
  await page.locator('#addExpense').click();
  await page.locator('#mobileFromSales').fill('300000');
  await page.locator('#actualCash').fill('10780000');
  await page.locator('#debtorName').fill('Darius');
  await page.locator('#debtAmount').fill('100000');
  await page.locator('#addDebt').click();

  // Total Sales = 110 × 79,000 + 25 × 100,000 = 11,190,000, and the allocation
  // Cash + Mobile Money + New Debt must add up to exactly that.
  await expect(page.locator('#summaryCards')).toContainText('UGX 11,190,000');
  await expect(page.locator('#tenderHint')).toContainText('Cash from today\'s sales: UGX 10,790,000');
  await expect(page.locator('#cashEquation')).toContainText('Cash at Hand UGX 10,780,000');

  const closed = await closeDay(page);
  expect(closed.status(), await closed.text()).toBe(200);
  await expect(page.locator('#statusPill')).toHaveText('CLOSED');
  await expect(page.locator('#closeDay')).toBeHidden();

  const stored = await dayJson(page, BUSINESS_DATE);
  expect(stored.day.status).toBe('closed');
  expect(stored.totals.total_sales_ugx).toBe(11190000);
  expect(stored.totals.mobile_money_from_sales_ugx).toBe(300000);
  expect(stored.totals.new_debt_ugx).toBe(100000);
  expect(stored.totals.cash_from_sales_ugx).toBe(10790000);
  expect(stored.totals.total_expenses_ugx).toBe(10000);
  expect(stored.totals.cash_at_hand_ugx).toBe(10780000);
  expect(stored.totals.cash_variance_ugx).toBe(0);
  expect(stored.expenses.map((e) => e.description)).toEqual(['Lunch']);
  expect(stored.debts.map((d) => d.name)).toEqual(['Darius']);
  expect(stored.invoices.length).toBe(2);

  // Reloading the page and the date shows exactly what was stored.
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Daily accountability' })).toBeVisible();
  await openDay(page, BUSINESS_DATE);
  await expect(page.locator('#statusPill')).toHaveText('CLOSED');
  await expect(page.locator('#expenseList')).toContainText('Lunch');
  await expect(page.locator('#debtList')).toContainText('Darius');
  await expect(page.locator('#mobileFromSales')).toHaveValue('300000');
  await expect(page.locator('#actualCash')).toHaveValue('10780000');
  await expect(salesCard(page, 'Compact Standard').locator('[data-card-field="quantity"]').first())
    .toHaveValue('110');
});

test('E: a refused Close day writes nothing and keeps the typed figures', async ({ page }) => {
  await loginAsSalesOperator(page);
  await openDay(page, NEXT_DATE);
  await stockRow(page, 'Compact Standard').locator('[data-stock-field="returned"]').fill('35');
  await stockRow(page, 'Napkins Standard').locator('[data-stock-field="returned"]').fill('5');
  const compact = salesCard(page, 'Compact Standard');
  await compact.locator('[data-card-field="quantity"]').first().fill('3');   // 5 were sold
  await compact.locator('[data-card-field="unit_price_ugx"]').first().fill('79000');
  await page.locator('#actualCash').fill('1000');

  const refused = await closeDay(page);
  expect(refused.status()).toBe(400);
  await expect(page.locator('#message')).toContainText('were sold');

  // Nothing was stored, and every typed figure is still on screen.
  const stored = await dayJson(page, NEXT_DATE);
  expect(stored.day).toBeNull();
  expect(stored.invoices).toEqual([]);
  expect(stored.totals.total_sales_ugx).toBe(0);
  await expect(compact.locator('[data-card-field="quantity"]').first()).toHaveValue('3');
  await expect(stockRow(page, 'Compact Standard').locator('[data-stock-field="returned"]'))
    .toHaveValue('35');
  await expect(page.locator('#actualCash')).toHaveValue('1000');
  await expect(page.locator('#statusPill')).not.toHaveText('CLOSED');
});



test('F/G/L/M: every card has an X, a closed day protects its history, no overflow', async ({ page }) => {
  await loginAsSalesOperator(page);
  const writes = [];
  page.on('request', (request) => {
    if (request.method() !== 'GET' && request.url().includes('/api/sales/')) {
      writes.push(`${request.method()} ${request.url()}`);
    }
  });

  await openDay(page, '2026-11-05');
  // Every Vehicle Stock card carries the compact ×, including the ones loaded
  // from an earlier count (not just the ones added by hand).
  const total = await page.locator('.vstock-row').count();
  expect(total).toBeGreaterThan(0);
  await expect(page.locator('.vstock-row [data-stock-remove]')).toHaveCount(total);

  // Removing a card is a browser-only cancel: the card leaves, the product is
  // selectable again and nothing is written.
  await stockRow(page, 'Compact Standard').locator('[data-stock-remove]').click();
  await expect(page.locator('.vstock-row').filter({ hasText: 'Compact Standard' })).toHaveCount(0);
  await page.locator('#addStockProduct').click();
  const picker = page.locator('[data-stock-pick]').last();
  const pickerHtml = await picker.innerHTML();
  expect(pickerHtml, pickerHtml).toContain('Compact Standard');
  expect(writes).toEqual([]);

  // The expense × is just as local and never resets the rest of the page.
  await showForm(page, 'expenseForm');
  await page.locator('#expenseDescription').fill('Lunch');
  await page.locator('#expenseAmount').fill('10000');
  await page.locator('#addExpense').click();
  await page.locator('#expenseDescription').fill('Fuel');
  await page.locator('#expenseAmount').fill('20000');
  await page.locator('#addExpense').click();
  await expect(page.locator('#expenseList .list-row')).toHaveCount(2);
  await page.locator('#mobileFromSales').fill('50000');
  await page.locator('#actualCash').fill('7000');
  await page.locator('#expenseList [data-remove-expense]').first().click();
  await expect(page.locator('#expenseList .list-row')).toHaveCount(1);
  await expect(page.locator('#expenseList')).toContainText('Fuel');
  await expect(page.locator('#mobileFromSales')).toHaveValue('50000');
  await expect(page.locator('#actualCash')).toHaveValue('7000');
  expect(writes).toEqual([]);

  // On a CLOSED day the × is still visible, but it refuses to destroy history.
  // Close this test's own day first so the check never depends on another test.
  await openDay(page, '2026-11-06');
  await countEverythingBack(page);
  await page.locator('#actualCash').fill('0');
  const closedSix = await closeDay(page);
  expect(closedSix.status(), await closedSix.text()).toBe(200);
  await expect(page.locator('#statusPill')).toHaveText('CLOSED');
  const closedRows = await page.locator('.vstock-row').count();
  await expect(page.locator('.vstock-row [data-stock-remove]')).toHaveCount(closedRows);
  const writesBeforeX = writes.length;
  await stockRow(page, 'Compact Standard').locator('[data-stock-remove]').click();
  await expect(page.locator('#message')).toContainText('reopened by a Manager');
  expect(writes.length, writes.join('\n')).toBe(writesBeforeX);

  // No horizontal overflow at a 375 × 812 viewport.
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
});



test('H/I/J/K: an unpaid sale, then collections on later dates', async ({ page }) => {
  await loginAsSalesOperator(page);

  // 10 Nov: one carton sold entirely on credit.  The carried figure is whatever
  // the vehicle holds, so the test works from the sheet it can see.
  await openDay(page, '2026-11-10');
  const compactTotal = await sheetTotal(page, 'Compact Standard');
  await stockRow(page, 'Compact Standard').locator('[data-stock-field="returned"]').fill(String(compactTotal - 1));
  await stockRow(page, 'Napkins Standard').locator('[data-stock-field="returned"]').fill(String(await sheetTotal(page, 'Napkins Standard')));
  const compact = salesCard(page, 'Compact Standard');
  await compact.locator('[data-card-field="quantity"]').first().fill('1');
  await compact.locator('[data-card-field="unit_price_ugx"]').first().fill('100000');
  await page.locator('#debtorName').fill('Musa');
  await page.locator('#debtAmount').fill('100000');
  await page.locator('#addDebt').click();
  await page.locator('#actualCash').fill('0');
  // Cash 0 + Mobile 0 + New Debt 100,000 = Total Sales 100,000.
  await expect(page.locator('#summaryCards')).toContainText('UGX 100,000');
  await expect(page.locator('#tenderHint')).toContainText('Cash from today\'s sales: UGX 0');
  expect((await closeDay(page)).status()).toBe(200);
  await expect(page.locator('#debtList')).toContainText('Unpaid');

  // 11 Nov: 40,000 of it arrives in cash.  No product sold today.
  await openDay(page, '2026-11-11');
  await countEverythingBack(page);
  await chooseDebtor(page, 'Musa');
  await page.locator('#collectionAmount').fill('40000');
  await page.locator('#collectionMethod').selectOption('cash');
  await page.locator('#addCollection').click();
  await expect(page.locator('#debtList')).toContainText('Partially Paid');
  await expect(page.locator('#summaryCards')).toContainText('UGX 0');
  await page.locator('#actualCash').fill('40000');
  const openingEleven = (await dayJson(page, '2026-11-11')).opening_balances.cash_ugx;
  const elevenClose = await closeDay(page);
  expect(elevenClose.status(), await elevenClose.text()).toBe(200);
  const eleven = await dayJson(page, '2026-11-11');
  expect(eleven.totals.total_sales_ugx).toBe(0);
  expect(eleven.totals.old_debt_collections_ugx).toBe(40000);
  // The collection is money received today on top of what the van already held.
  expect(eleven.totals.expected_cash_ugx).toBe(openingEleven + 40000);

  // 12 Nov: the remaining 60,000 arrives on Mobile Money; the debt is Paid.
  await openDay(page, '2026-11-12');
  await countEverythingBack(page);
  await chooseDebtor(page, 'Musa');
  await page.locator('#collectionAmount').fill('60000');
  await page.locator('#collectionMethod').selectOption('mobile_money');
  await page.locator('#addCollection').click();
  await expect(page.locator('#debtList')).toContainText('Paid');
  await page.locator('#actualCash').fill('40000');
  await page.locator('#actualMobile').fill('60000');
  const openingTwelve = (await dayJson(page, '2026-11-12')).opening_balances.mobile_money_ugx;
  const twelveClose = await closeDay(page);
  expect(twelveClose.status(), await twelveClose.text()).toBe(200);
  const twelve = await dayJson(page, '2026-11-12');
  expect(twelve.totals.total_sales_ugx).toBe(0);
  expect(twelve.totals.old_debt_mobile_money_ugx).toBe(60000);
  expect(twelve.totals.expected_mobile_money_ugx).toBe(openingTwelve + 60000);
  expect((twelve.debtor_balances || []).filter((row) => row.name === 'Musa')).toEqual([]);

  // 10 November is untouched: the sale and its stock stay exactly as closed.
  const ten = await dayJson(page, '2026-11-10');
  expect(ten.totals.total_sales_ugx).toBe(100000);
  expect(ten.totals.old_debt_collections_ugx).toBe(0);
  const stock = (ten.stock || []).find((row) => row.product_name === 'Compact Standard');
  expect(stock.derived_sold.cartons).toBe(1);
});

// Put Compact Standard's four Vehicle Stock figures into a known state by typing
// into the boxes directly, independent of whatever a date happened to carry
// forward. Previous 50 + Given 100 = Total 150, Return 40 -> Sold 110 cartons,
// matching the paper sheet this feature was built around.
async function sellOneTenCartons(page) {
  const compact = stockRow(page, 'Compact Standard');
  await compact.locator('select[data-stock-unit]').selectOption('carton');
  await compact.locator('[data-stock-field="previous"]').fill('50');
  await compact.locator('[data-stock-field="issued"]').fill('100');
  await compact.locator('[data-stock-field="returned"]').fill('40');
  await expect(compact.locator('[data-stock-sold]')).toHaveText('110 cartons');
  return compact;
}

test('N: a default-zero Vehicle Stock field lets typing replace the zero, decimals unaffected', async ({ page }) => {
  await loginAsSalesOperator(page);
  await openDay(page, '2026-11-20');

  // Given Stock / Issued Today starts at a literal "0" whenever nothing was
  // dispatched today - exactly the field from the bug report.
  const compact = stockRow(page, 'Compact Standard');
  const issued = compact.locator('[data-stock-field="issued"]');
  await expect(issued).toHaveValue('0');
  await issued.click();
  await issued.pressSequentially('100');
  await expect(issued).toHaveValue('100');
  // Never "0100".
  await expect(issued).not.toHaveValue('0100');

  // A second Vehicle Stock field: Previous Return, deliberately set to zero,
  // then focused and typed into the same way.
  const previous = compact.locator('[data-stock-field="previous"]');
  await previous.fill('0');
  await previous.blur();                 // a fresh focus, like a salesperson tapping back in
  await previous.click();
  await previous.pressSequentially('45');
  await expect(previous).toHaveValue('45');
  await expect(previous).not.toHaveValue('045');

  // A legitimate non-zero value is left alone by merely focusing it again.
  await issued.click();
  await expect(issued).toHaveValue('100');

  // Decimal Sales Card quantities (0.5 of a carton) still work normally.
  const qty = salesCard(page, 'Compact Standard').locator('[data-card-field="quantity"]').first();
  await qty.click();
  await qty.pressSequentially('0.5');
  await expect(qty).toHaveValue('0.5');
});

test('O: no yellow stock-vs-sales banner, but Close Day still enforces the exact match', async ({ page }) => {
  await loginAsSalesOperator(page);
  await openDay(page, '2026-11-21');
  await countEverythingBack(page);                 // every other product: nothing sold
  await sellOneTenCartons(page);

  const compact = salesCard(page, 'Compact Standard');
  await compact.locator('[data-card-field="quantity"]').first().fill('100');
  await compact.locator('[data-card-field="unit_price_ugx"]').first().fill('79000');

  // The Sales Card says what is left to price...
  await expect(compact.locator('.card-status')).toContainText('10 cartons still to price');
  // ...and the removed yellow banner is nowhere in the DOM, not merely hidden.
  await expect(page.locator('#stockWarnings')).toHaveCount(0);
  await expect(page.locator('.warning:visible')).toHaveCount(0);

  // The authoritative mismatch check still lives on the server: Close Day is
  // refused while 10 cartons remain unpriced, and nothing is stored.
  const refused = await closeDay(page);
  expect(refused.status()).toBe(400);
  await expect(page.locator('#message')).toContainText('were sold');
  const stored = await dayJson(page, '2026-11-21');
  expect(stored.day).toBeNull();
});

test('P: Add another price stays usable and multiple prices sum to Sold Today', async ({ page }) => {
  await loginAsSalesOperator(page);
  await openDay(page, '2026-11-22');

  const addBtn = salesCard(page, 'Compact Standard').locator('[data-card-add]');
  // Before Sold Today is known (Return Today not counted yet), the control is
  // correctly unusable...
  await expect(addBtn).toBeDisabled();

  // ...and becomes usable the moment Sold Today is known, without a full page
  // reload or re-opening the card - the exact regression this fixes.
  await countEverythingBack(page);
  await sellOneTenCartons(page);
  await expect(addBtn).toBeEnabled();

  const card = salesCard(page, 'Compact Standard');
  await card.locator('[data-card-field="quantity"]').first().fill('100');
  await card.locator('[data-card-field="unit_price_ugx"]').first().fill('79000');
  await expect(card.locator('.card-status')).toContainText('10 cartons still to price');
  await expect(addBtn).toBeEnabled();              // still usable with only 100 of 110 priced

  await addBtn.click();
  await expect(card.locator('.price-row')).toHaveCount(2);
  await card.locator('[data-card-field="quantity"]').nth(1).fill('10');
  await card.locator('[data-card-field="unit_price_ugx"]').nth(1).fill('80000');
  await expect(card.locator('.card-status')).toContainText('matches the stock count');
  await expect(page.locator('#summaryCards')).toContainText('UGX 8,700,000');

  // Removing the second row recalculates everything immediately.
  await card.locator('[data-card-remove]').nth(1).click();
  await expect(card.locator('.price-row')).toHaveCount(1);
  await expect(card.locator('.card-status')).toContainText('10 cartons still to price');
  await expect(page.locator('#summaryCards')).toContainText('UGX 7,900,000');

  // Re-adding and completing it again works the same way.
  await addBtn.click();
  await card.locator('[data-card-field="quantity"]').nth(1).fill('10');
  await card.locator('[data-card-field="unit_price_ugx"]').nth(1).fill('80000');
  await expect(card.locator('.card-status')).toContainText('matches the stock count');
  await expect(page.locator('#summaryCards')).toContainText('UGX 8,700,000');

  await page.locator('#actualCash').fill('8700000');
  const closed = await closeDay(page);
  expect(closed.status(), await closed.text()).toBe(200);
  const stored = await dayJson(page, '2026-11-22');
  expect(stored.product_cards[0].rows.map((r) => r.quantity).sort()).toEqual([10, 100]);
});

test('Q: decimal multi-price rows sum exactly, with no floating-point drift', async ({ page }) => {
  await loginAsSalesOperator(page);
  await openDay(page, '2026-11-23');
  await countEverythingBack(page);

  // 15 packs issued and none returned: 1.5 cartons sold, same packaging rule
  // used on the server side of this fix (10 packs per carton).
  const compact = stockRow(page, 'Compact Standard');
  await compact.locator('select[data-stock-unit]').selectOption('pack');
  await compact.locator('[data-stock-field="previous"]').fill('0');
  await compact.locator('[data-stock-field="issued"]').fill('15');
  await compact.locator('[data-stock-field="returned"]').fill('0');

  const card = salesCard(page, 'Compact Standard');
  await card.locator('[data-card-field="quantity"]').first().fill('1');
  await card.locator('[data-card-field="unit_price_ugx"]').first().fill('79000');
  await card.locator('[data-card-add]').click();
  await card.locator('[data-card-field="quantity"]').nth(1).fill('0.5');
  await card.locator('[data-card-field="unit_price_ugx"]').nth(1).fill('80000');
  await expect(card.locator('.card-status')).toContainText('matches the stock count');
  await expect(page.locator('#summaryCards')).toContainText('UGX 119,000');

  await page.locator('#actualCash').fill('119000');
  const closed = await closeDay(page);
  expect(closed.status(), await closed.text()).toBe(200);
  const stored = await dayJson(page, '2026-11-23');
  expect(stored.totals.total_sales_ugx).toBe(119000);
  const stock = (stored.stock || []).find((row) => row.product_name === 'Compact Standard');
  expect(stock.derived_sold.base_unit_qty).toBe(150);
});

test('R: an overpriced card is refused at Close Day, nothing persists, no silent clamp', async ({ page }) => {
  await loginAsSalesOperator(page);
  await openDay(page, '2026-11-24');
  await countEverythingBack(page);
  await sellOneTenCartons(page);                   // 110 cartons sold

  const card = salesCard(page, 'Compact Standard');
  await card.locator('[data-card-field="quantity"]').first().fill('100');
  await card.locator('[data-card-field="unit_price_ugx"]').first().fill('79000');
  await card.locator('[data-card-add]').click();
  await card.locator('[data-card-field="quantity"]').nth(1).fill('20');  // 120 total, 10 too many
  await card.locator('[data-card-field="unit_price_ugx"]').nth(1).fill('80000');
  await expect(card.locator('.card-status')).toContainText('remove');
  await expect(card.locator('.tag').first()).toHaveText('Too much');

  const refused = await closeDay(page);
  expect(refused.status()).toBe(400);
  await expect(page.locator('#message')).toContainText('only');

  const stored = await dayJson(page, '2026-11-24');
  expect(stored.day).toBeNull();
  // The rows on screen were never silently clamped to 110.
  await expect(card.locator('[data-card-field="quantity"]').nth(1)).toHaveValue('20');
});

test('S: Total Sales Today is the single final summary card, live at desktop and mobile', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await loginAsSalesOperator(page);
  await openDay(page, '2026-11-25');

  // Exactly one summary card, and it is the last "step" section on the page.
  await expect(page.getByRole('heading', { name: 'Total sales today' })).toHaveCount(1);
  const headings = (await page.locator('#dailyView > section.card h2').allTextContents())
    .map((t) => t.replace(/\s+/g, ' ').trim());
  expect(headings[headings.length - 1]).toContain('Total sales today');
  expect(headings.indexOf('7 Total sales today'))
    .toBeGreaterThan(headings.indexOf('6 Cash at hand'));

  await countEverythingBack(page);
  await sellOneTenCartons(page);
  const card = salesCard(page, 'Compact Standard');
  await card.locator('[data-card-field="quantity"]').first().fill('110');
  await card.locator('[data-card-field="unit_price_ugx"]').first().fill('79000');
  // 110 x 79,000, live, at desktop width.
  await expect(page.locator('#summaryCards')).toContainText('UGX 8,690,000');

  // Same page, mobile viewport: still one card, still last, no horizontal
  // overflow, no yellow banner, and the live total survives the resize.
  await page.setViewportSize({ width: 375, height: 812 });
  await expect(page.getByRole('heading', { name: 'Total sales today' })).toHaveCount(1);
  await expect(page.locator('#summaryCards')).toContainText('UGX 8,690,000');
  await expect(page.locator('#stockWarnings')).toHaveCount(0);
  const addBtn = card.locator('[data-card-add]');
  await expect(addBtn).toBeEnabled();
  await addBtn.click();
  await expect(card.locator('.price-row')).toHaveCount(2);
  await expect(card.locator('[data-card-remove]').last()).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
});

test('T: Save persists an unbalanced day; Close Day refuses it until corrected; Save sits beside Close Day, bottom-right', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await loginAsSalesOperator(page);
  await openDay(page, '2026-11-26');

  // The final card is still "Total sales today", and Save/Close day now live
  // inside it, bottom-right, in that order.
  const headings = (await page.locator('#dailyView > section.card h2').allTextContents())
    .map((t) => t.replace(/\s+/g, ' ').trim());
  expect(headings[headings.length - 1]).toContain('Total sales today');
  const finalCard = page.locator('#dailyView > section.card').last();
  await expect(finalCard.locator('#saveDay')).toBeVisible();
  await expect(finalCard.locator('#closeDay')).toBeVisible();
  const saveBox = await finalCard.locator('#saveDay').boundingBox();
  const closeBox = await finalCard.locator('#closeDay').boundingBox();
  expect(saveBox.x).toBeLessThan(closeBox.x);
  // An Operator has no reopen/correction authority on this (never-closed) day.
  await expect(page.locator('#reopenDay')).toBeHidden();

  // Enter an unbalanced day: a sales card priced for less than Sold Today,
  // and no cash counted yet.  Every other product is counted back in full so
  // only the one priced card is what keeps this day from balancing.
  await countEverythingBack(page);
  await sellOneTenCartons(page);
  const card = salesCard(page, 'Compact Standard');
  await card.locator('[data-card-field="quantity"]').first().fill('6');
  await card.locator('[data-card-field="unit_price_ugx"]').first().fill('79000');

  const saved = await saveWorkingDay(page);
  expect(saved.status(), await saved.text()).toBe(200);
  await expect(page.locator('#message')).toContainText('Day saved.');
  await expect(page.locator('#statusPill')).toHaveText('SAVED · UNBALANCED');

  // Navigate away and back: the saved, unbalanced figures reload exactly.
  await openDay(page, '2026-11-27');
  await openDay(page, '2026-11-26');
  await expect(page.locator('#statusPill')).toHaveText('SAVED · UNBALANCED');
  await expect(stockRow(page, 'Compact Standard').locator('[data-stock-field="returned"]')).toHaveValue('40');
  await expect(salesCard(page, 'Compact Standard').locator('[data-card-field="quantity"]').first())
    .toHaveValue('6');

  // A full page reload (not just a date change) reproduces it too.
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Daily accountability' })).toBeVisible();
  await openDay(page, '2026-11-26');
  await expect(page.locator('#statusPill')).toHaveText('SAVED · UNBALANCED');
  await expect(salesCard(page, 'Compact Standard').locator('[data-card-field="quantity"]').first())
    .toHaveValue('6');

  // Close Day still refuses while the day is unbalanced.
  const refused = await closeDay(page);
  expect(refused.status()).toBe(400);
  await expect(page.locator('#message')).toContainText('were sold');
  await expect(page.locator('#statusPill')).toHaveText('SAVED · UNBALANCED');

  // Correct the card (100 @ 79,000 + 10 @ 80,000 = the full 110 sold) and
  // count the cash, Save again, then Close Day succeeds.
  const cardAgain = salesCard(page, 'Compact Standard');
  await cardAgain.locator('[data-card-field="quantity"]').first().fill('100');
  await cardAgain.locator('[data-card-add]').click();
  await cardAgain.locator('[data-card-field="quantity"]').nth(1).fill('10');
  await cardAgain.locator('[data-card-field="unit_price_ugx"]').nth(1).fill('80000');
  await page.locator('#actualCash').fill('8700000');
  const savedAgain = await saveWorkingDay(page);
  expect(savedAgain.status(), await savedAgain.text()).toBe(200);
  await expect(page.locator('#statusPill')).toHaveText('SAVED · BALANCED');
  const closed = await closeDay(page);
  expect(closed.status(), await closed.text()).toBe(200);
  await expect(page.locator('#statusPill')).toHaveText('CLOSED');

  // The closed date stays reachable and its data remains available.
  await openDay(page, '2026-11-27');
  await openDay(page, '2026-11-26');
  await expect(page.locator('#statusPill')).toHaveText('CLOSED');
  await expect(salesCard(page, 'Compact Standard').locator('.price-row')).toHaveCount(2);

  // No horizontal overflow at 375 × 812, with everything above still showing.
  await page.setViewportSize({ width: 375, height: 812 });
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  await expect(page.locator('#statusPill')).toHaveText('CLOSED');
  await expect(finalCard.locator('#saveDay')).toBeHidden();
  await expect(finalCard.locator('#closeDay')).toBeHidden();
});

test('U: pressing Save repeatedly with the same figures writes no duplicate rows', async ({ page }) => {
  await loginAsSalesOperator(page);
  await openDay(page, '2026-11-28');
  await sellOneTenCartons(page);
  const card = salesCard(page, 'Compact Standard');
  await card.locator('[data-card-field="quantity"]').first().fill('10');
  await card.locator('[data-card-field="unit_price_ugx"]').first().fill('79000');
  await showForm(page, 'expenseForm');
  await page.locator('#expenseDescription').fill('Fuel');
  await page.locator('#expenseAmount').fill('5000');
  await page.locator('#addExpense').click();

  const first = await saveWorkingDay(page);
  expect(first.status(), await first.text()).toBe(200);
  const firstStored = await dayJson(page, '2026-11-28');
  expect(firstStored.expenses.length).toBe(1);
  expect(firstStored.invoices.length).toBe(1);
  const firstExpenseId = firstStored.expenses[0].id;
  const firstInvoiceId = firstStored.invoices[0].id;

  for (let i = 0; i < 3; i += 1) {
    const again = await saveWorkingDay(page);
    expect(again.status(), await again.text()).toBe(200);
  }

  const afterStored = await dayJson(page, '2026-11-28');
  expect(afterStored.expenses.length).toBe(1);
  expect(afterStored.invoices.length).toBe(1);
  expect(afterStored.expenses[0].id).toBe(firstExpenseId);
  expect(afterStored.invoices[0].id).toBe(firstInvoiceId);
});

test('V: the Reconciliation data reports a saved-unbalanced date distinctly from a closed-balanced one', async ({ page }) => {
  await loginAsSalesOperator(page);
  await openDay(page, '2026-11-29');
  await sellOneTenCartons(page);
  const unbalancedCard = salesCard(page, 'Compact Standard');
  await unbalancedCard.locator('[data-card-field="quantity"]').first().fill('6');
  await unbalancedCard.locator('[data-card-field="unit_price_ugx"]').first().fill('79000');
  const saved = await saveWorkingDay(page);
  expect(saved.status(), await saved.text()).toBe(200);

  await openDay(page, '2026-11-30');
  await countEverythingBack(page);
  await sellOneTenCartons(page);                           // 110 cartons sold
  const balancedCard = salesCard(page, 'Compact Standard');
  await balancedCard.locator('[data-card-field="quantity"]').first().fill('110');
  await balancedCard.locator('[data-card-field="unit_price_ugx"]').first().fill('79000');
  await page.locator('#actualCash').fill('8690000');
  const closed = await closeDay(page);
  expect(closed.status(), await closed.text()).toBe(200);

  // The Reconciliation history is server-authoritative, never frontend
  // arithmetic: the same session's own fetch is what a Reconciliation list
  // reads from.
  const history = await page.evaluate(async () => {
    const res = await fetch('/api/sales/history?date_from=2026-11-29&date_to=2026-11-30');
    return res.json();
  });
  const byDate = Object.fromEntries(history.map((row) => [row.business_date, row]));
  expect(byDate['2026-11-29'].status_code).toBe('open');
  expect(byDate['2026-11-29'].balanced).toBe(false);
  expect(byDate['2026-11-30'].status_code).toBe('closed');
  expect(byDate['2026-11-30'].balanced).toBe(true);
});

test('W: a debtor partial payment is collected on a later, still-unbalanced Saved day', async ({ page }) => {
  await loginAsSalesOperator(page);
  await openDay(page, '2026-12-01');
  await countEverythingBack(page);
  await sellOneTenCartons(page);                           // 110 cartons sold
  const card = salesCard(page, 'Compact Standard');
  await card.locator('[data-card-field="quantity"]').first().fill('110');
  await card.locator('[data-card-field="unit_price_ugx"]').first().fill('79000');
  await page.locator('#debtorName').fill('Grace');
  await page.locator('#debtAmount').fill('79000');          // one carton's worth, left unpaid
  await page.locator('#addDebt').click();
  await page.locator('#actualCash').fill('8611000');        // 8,690,000 total − 79,000 debt
  const closed = await closeDay(page);
  expect(closed.status(), await closed.text()).toBe(200);
  await expect(page.locator('#debtList')).toContainText('Unpaid');

  // A later date: part of the debt arrives in cash.  Save it without
  // balancing the day (nothing sold today, cash not counted yet).
  await openDay(page, '2026-12-02');
  await chooseDebtor(page, 'Grace');
  await page.locator('#collectionAmount').fill('30000');
  await page.locator('#collectionMethod').selectOption('cash');
  await page.locator('#addCollection').click();
  await expect(page.locator('#debtList')).toContainText('Partially Paid');
  const saved = await saveWorkingDay(page);
  expect(saved.status(), await saved.text()).toBe(200);
  await expect(page.locator('#statusPill')).toHaveText('SAVED · UNBALANCED');

  // Navigate away and back: the collection and the remaining balance both
  // reload exactly, with no double-counting against the server's own balance.
  await openDay(page, '2026-12-03');
  await openDay(page, '2026-12-02');
  await expect(page.locator('#debtList')).toContainText('Partially Paid');
  await expect(page.locator('#debtList')).toContainText('UGX 49,000');  // 79,000 − 30,000 still owed

  // Finish collecting the rest by Mobile Money: fully paid, live, before Save.
  await chooseDebtor(page, 'Grace');
  await page.locator('#collectionAmount').fill('49000');
  await page.locator('#collectionMethod').selectOption('mobile_money');
  await page.locator('#addCollection').click();
  await expect(page.locator('[data-debtor-balance]').filter({ hasText: 'Grace' })).toContainText('Paid');
  await page.locator('#actualCash').fill('30000');
  await page.locator('#actualMobile').fill('49000');
  const finalSave = await saveWorkingDay(page);
  expect(finalSave.status(), await finalSave.text()).toBe(200);
  // Fully settled: Grace drops off the outstanding list entirely, exactly
  // like an existing closed debt that reaches zero (no debtor stays listed
  // as "Paid" forever - there is simply nothing left to collect).
  await expect(page.locator('[data-debtor-balance]').filter({ hasText: 'Grace' })).toHaveCount(0);
  const balances = (await dayJson(page, '2026-12-02')).debtor_balances || [];
  expect(balances.filter((row) => row.name === 'Grace')).toEqual([]);

  // The collection never becomes a second sale, on either date.
  const day1 = await dayJson(page, '2026-12-01');
  expect(day1.totals.total_sales_ugx).toBe(8690000);
  const day2 = await dayJson(page, '2026-12-02');
  expect(day2.totals.total_sales_ugx).toBe(0);
});
