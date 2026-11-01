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
