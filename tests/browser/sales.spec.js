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
}

async function openDay(page, date) {
  await page.locator('#businessDate').fill(date);
  await Promise.all([
    page.waitForResponse(r => r.url().endsWith('/api/sales/days') && r.request().method() === 'POST'),
    page.locator('#loadDay').click(),
  ]);
}

async function fillPriceRow(card, index, quantity, price) {
  const row = card.locator('.price-row').nth(index);
  await row.locator('[data-card-field="quantity"]').fill(String(quantity));
  await row.locator('[data-card-field="unit_price_ugx"]').fill(String(price));
}

async function addPriceRow(card, quantity, price) {
  const before = await card.locator('.price-row').count();
  await card.locator('[data-card-add]').click();
  await fillPriceRow(card, before, quantity, price);
}

async function dayJson(page, date) {
  return page.evaluate(async (d) => (await (await fetch(`/api/sales/days/${d}`)).json()), date);
}

async function stockOf(page, date, productName) {
  const day = await dayJson(page, date);
  return day.stock.find((row) => row.product_name === productName);
}

async function addExpense(page, description, amount) {
  await page.locator('#expenseDescription').fill(description);
  await page.locator('#expenseAmount').fill(String(amount));
  await Promise.all([
    page.waitForResponse(r => r.url().includes('/expenses') && r.request().method() === 'POST'),
    page.locator('#addExpense').click(),
  ]);
}

async function showExpenseForm(page) {
  const form = page.locator('#expenseForm');
  if (!(await form.isVisible())) await page.locator('[data-toggle-form="expenseForm"]').click();
  await expect(form).toBeVisible();
}


test('Salesperson works live during the day and saves everything at Close Day', async ({ page }) => {
  await loginAsSalesOperator(page);

  // Every non-GET request to the Vehicle stock sheet is recorded: the sheet and
  // the sales cards must never leave the browser before Close Day.
  const stockWrites = [];
  page.on('request', (request) => {
    if (request.url().includes('/vehicle-stock') && request.method() !== 'GET') {
      stockWrites.push(`${request.method()} ${request.url()}`);
    }
  });
  const cardWrites = [];
  page.on('request', (request) => {
    if (request.url().includes('/products/') && request.url().endsWith('/sales')) {
      cardWrites.push(`${request.method()} ${request.url()}`);
    }
  });

  // The six steps, in the order a salesperson works. Sold Today has no section
  // of its own: it lives in its Vehicle stock card.
  const headings = await page.locator('#dailyView .card h2').allTextContents();
  expect(headings.map(h => h.replace(/^\s*\d+\s*/, '').trim())).toEqual([
    'Vehicle stock',
    'Sales cards',
    'Total sales today',
    'Mobile Money',
    'Expenses and money movement',
    'Cash at hand',
  ]);
  await expect(page.locator('#profileWrap')).toBeHidden();
  await expect(page.locator('#soldList')).toHaveCount(0);
  await expect(page.locator('#saveVehicleStock')).toHaveCount(0);
  // Nothing is saved per product any more: Close Day is the one save.
  await expect(page.locator('[data-card-save]')).toHaveCount(0);

  await openDay(page, BUSINESS_DATE);
  await expect(page.locator('#statusPill')).toHaveText('DRAFT');
  await expect(page.locator('.vstock-row')).toHaveCount(2);

  // ---- Step 1: the editable Vehicle stock sheet --------------------------
  const compactStock = stockRow(page, 'Compact Standard');
  const napkinStock = stockRow(page, 'Napkins Standard');
  for (const label of ['Previous Return', 'Given Stock / Issued Today', 'Total Stock', 'Return Today', 'Sold Today']) {
    await expect(compactStock).toContainText(label);
  }
  await expect(compactStock.locator('[data-stock-field="previous"]')).toHaveValue('50');
  await expect(compactStock.locator('[data-stock-field="issued"]')).toHaveValue('100');
  await expect(compactStock.locator('[data-stock-field="total"]')).toHaveValue('150');
  await expect(compactStock.locator('[data-stock-field="returned"]')).toHaveValue('');
  await expect(napkinStock.locator('[data-stock-field="previous"]')).toHaveValue('10');
  await expect(napkinStock.locator('[data-stock-field="issued"]')).toHaveValue('20');
  await expect(napkinStock.locator('[data-stock-field="total"]')).toHaveValue('30');
  await expect(page.locator('#openingNotice')).toBeHidden();
  // Test G — the Unit control is a Vehicle stock field and stays here.
  await expect(compactStock.locator('[data-stock-unit]')).toHaveCount(1);

  // 52 + 100 = 152 by hand, corrected to 150, less 40 returns = 110 sold.
  await compactStock.locator('[data-stock-field="previous"]').fill('52');
  await expect(compactStock.locator('[data-stock-field="total"]')).toHaveValue('152');
  await compactStock.locator('[data-stock-field="total"]').fill('150');
  await compactStock.locator('[data-stock-field="issued"]').fill('90');
  await expect(compactStock.locator('[data-stock-field="total"]')).toHaveValue('150');
  await compactStock.locator('[data-stock-field="issued"]').fill('100');
  await compactStock.locator('[data-stock-field="returned"]').fill('40');
  await expect(compactStock.locator('[data-stock-sold]')).toHaveText('110 cartons');
  await napkinStock.locator('[data-stock-field="returned"]').fill('5');
  await expect(napkinStock.locator('[data-stock-sold]')).toHaveText('25 cartons');
  // A sold quantity repaints the product's Sales Card immediately, with no save.
  await expect(salesCard(page, 'Compact Standard')).toContainText('110 cartons sold today, from Vehicle stock');
  await expect(salesCard(page, 'Napkins Standard')).toContainText('25 cartons sold today, from Vehicle stock');
  // The products that were already on the vehicle carry no remove control.
  await expect(page.locator('[data-stock-remove]')).toHaveCount(0);

  // ---- TEST A: X on a card that was added by mistake ---------------------
  await page.locator('#addStockProduct').click();
  await expect(page.locator('.vstock-row')).toHaveCount(3);
  const addedEmpty = page.locator('.vstock-row').nth(2);
  await expect(addedEmpty.locator('[data-stock-pick]')).toHaveValue('');
  await expect(addedEmpty.locator('[data-stock-remove]')).toBeVisible();
  await expect(page.locator('[data-stock-remove]')).toHaveCount(1);
  await addedEmpty.locator('[data-stock-remove]').click();
  await expect(page.locator('.vstock-row')).toHaveCount(2);
  await expect(page.locator('[data-stock-remove]')).toHaveCount(0);
  // Cancelling a card writes nothing at all.
  expect(stockWrites).toEqual([]);
  expect(await stockOf(page, BUSINESS_DATE, 'Compact Standard')).toMatchObject({ closing: null });

  // A catalogue-only product can still be added, and while it is on the sheet
  // it cannot be chosen a second time.
  await page.locator('#addStockProduct').click();
  const strawPicker = page.locator('.vstock-row').nth(2).locator('[data-stock-pick]');
  await expect(strawPicker.locator('option', { hasText: 'Straws' })).toHaveCount(1);
  await strawPicker.selectOption({ label: 'Straws' });
  const strawStock = stockRow(page, 'Straws');
  await expect(strawStock.locator('[data-stock-field="previous"]')).toHaveValue('0');
  await expect(strawStock.locator('[data-stock-remove]')).toBeVisible();
  await page.locator('#addStockProduct').click();
  const emptyAgain = page.locator('.vstock-row').nth(3);
  const offeredWhileActive = await emptyAgain.locator('[data-stock-pick] option').allTextContents();
  expect(offeredWhileActive).not.toContain('Straws');
  expect(offeredWhileActive).not.toContain('Compact Standard');
  await emptyAgain.locator('[data-stock-remove]').click();
  await expect(page.locator('.vstock-row')).toHaveCount(3);
  // X on the Straws card takes it off the sheet and frees the product again.
  await strawStock.locator('[data-stock-remove]').click();
  await expect(page.locator('.vstock-row')).toHaveCount(2);
  await expect(page.locator('[data-stock-remove]')).toHaveCount(0);
  expect(stockWrites).toEqual([]);
  await page.locator('#addStockProduct').click();
  const strawsAgain = page.locator('.vstock-row').nth(2).locator('[data-stock-pick]');
  await expect(strawsAgain.locator('option', { hasText: 'Straws' })).toHaveCount(1);
  await strawsAgain.selectOption({ label: 'Straws' });
  // Straws was on no earlier sheet: 0 carried + 2 given, 1 back = 1 sold.
  await stockRow(page, 'Straws').locator('[data-stock-field="issued"]').fill('2');
  await expect(stockRow(page, 'Straws').locator('[data-stock-field="total"]')).toHaveValue('2');
  await stockRow(page, 'Straws').locator('[data-stock-field="returned"]').fill('1');
  await expect(stockRow(page, 'Straws').locator('[data-stock-sold]')).toHaveText('1 carton');

  // ---- Step 2: Sales Cards are live, with no per-product save ------------
  const compactCard = salesCard(page, 'Compact Standard');
  const napkinCard = salesCard(page, 'Napkins Standard');
  const strawCard = salesCard(page, 'Straws');
  await expect(page.locator('.sales-card')).toHaveCount(3);
  // Test D — no per-product Save Sales buttons exist on any card.
  await expect(page.locator('[data-card-save]')).toHaveCount(0);
  await expect(page.getByText('Save Compact Standard sales', { exact: false })).toHaveCount(0);
  await expect(page.getByText('Save Napkins Standard sales', { exact: false })).toHaveCount(0);
  // Test G — the Sales Card has no Unit selector at all; the unit comes from
  // the product's Vehicle stock card and is shown as plain text.
  await expect(page.locator('.sales-card [data-card-field="unit"]')).toHaveCount(0);
  await expect(page.locator('.sales-card select')).toHaveCount(0);
  await expect(compactCard.locator('.price-row').first()).toContainText('Quantity (cartons)');

  // ---- TEST H: fractional quantities -------------------------------------
  // Compact: 150 less 149 = 1 carton sold, priced 0.5 + 0.25 + 0.25.
  await compactStock.locator('[data-stock-field="returned"]').fill('149');
  await expect(compactStock.locator('[data-stock-sold]')).toHaveText('1 carton');
  await expect(compactCard).toContainText('1 carton sold today, from Vehicle stock');
  await fillPriceRow(compactCard, 0, 0.5, 79000);
  await addPriceRow(compactCard, 0.25, 80000);
  await addPriceRow(compactCard, 0.25, 84000);
  await expect(compactCard.locator('.card-status')).toContainText('matches the stock count');
  await expect(compactCard.locator('.card-status')).toContainText('UGX 80,500');
  // Napkins: 20 × 103,000 and 5 × 104,000 = UGX 2,580,000 for 25 cartons.
  await fillPriceRow(napkinCard, 0, 20, 103000);
  await addPriceRow(napkinCard, 5, 104000);
  await expect(napkinCard.locator('.card-status')).toContainText('matches the stock count');
  // Straws: a catalogue product priced in halves of a carton.
  await fillPriceRow(strawCard, 0, 0.5, 5000);
  await addPriceRow(strawCard, 0.5, 5000);
  await expect(strawCard.locator('.card-status')).toContainText('matches the stock count');
  await expect(strawCard.locator('.card-status')).toContainText('UGX 5,000');
  // Test D — total sales today is the live sum, before any save at all.
  await expect(page.locator('#summaryCards')).toContainText('UGX 2,665,500');
  const liveDay = await dayJson(page, BUSINESS_DATE);
  expect(liveDay.totals.total_sales_ugx).toBe(0);       // nothing persisted yet
  expect(liveDay.product_cards).toEqual([]);
  expect(cardWrites).toEqual([]);
  expect(stockWrites).toEqual([]);

  // A fractional card that prices too much is still checked, not rounded.
  await compactCard.locator('.price-row').nth(2).locator('[data-card-field="quantity"]').fill('0.5');
  await expect(compactCard.locator('.card-status')).toContainText('but only 1 carton were sold');
  await expect(compactCard.locator('.card-status.bad')).toHaveCount(1);
  await compactCard.locator('.price-row').nth(2).locator('[data-card-field="quantity"]').fill('0.25');
  await expect(compactCard.locator('.card-status')).toContainText('matches the stock count');

  // 1.5 + 1.5 = 3 cartons: the same rule with a bigger fraction.
  await compactStock.locator('[data-stock-field="returned"]').fill('147');
  await expect(compactStock.locator('[data-stock-sold]')).toHaveText('3 cartons');
  await compactCard.locator('.price-row').nth(2).locator('[data-card-remove]').click();
  await compactCard.locator('.price-row').nth(1).locator('[data-card-remove]').click();
  await fillPriceRow(compactCard, 0, 1.5, 79000);
  await addPriceRow(compactCard, 1.5, 80000);
  await expect(compactCard.locator('.card-status')).toContainText('matches the stock count');
  await expect(compactCard.locator('.card-status')).toContainText('UGX 238,500');
  await expect(page.locator('#summaryCards')).toContainText('UGX 2,823,500');
  await expect(page.locator('#tenderHint')).toContainText('UGX 2,823,500');
  expect(cardWrites).toEqual([]);
  expect(stockWrites).toEqual([]);

  await expect(page.locator('.vstock-row')).toHaveCount(3);
  expect(stockWrites).toEqual([]);
  expect(cardWrites).toEqual([]);


  // ---- TEST B: the expense X must not reset the page ---------------------
  await showExpenseForm(page);
  const expenseRows = page.locator('#expenseList .list-row');
  for (const [description, amount] of [['Lunch', 10000], ['Fahad', 20000], ['Breakfast', 10000]]) {
    await addExpense(page, description, amount);
  }
  await expect(expenseRows).toHaveCount(3);
  await expect(page.locator('#summaryCards')).toContainText('UGX 40,000');       // total expenses
  await expect(page.locator('#expectedCashBig')).toHaveText('UGX 2,783,500');    // 2,823,500 − 40,000
  // No confirmation popup and no page-wide reload: the old "Void expense"
  // button is gone, replaced by the compact X.
  await expect(page.getByText('Void expense', { exact: true })).toHaveCount(0);
  const dialogs = [];
  page.on('dialog', async (dialog) => { dialogs.push(dialog.message()); await dialog.dismiss(); });

  // Browser-only work that must survive the removal: a changed sheet figure
  // (Sold Today unchanged), the typed Mobile Money and counted cash.
  await compactStock.locator('[data-stock-field="issued"]').fill('101');
  await expect(compactStock.locator('[data-stock-sold]')).toHaveText('3 cartons');
  await page.locator('#mobileFromSales').fill('500000');
  await page.locator('#actualCash').fill('2823500');
  // The middle expense is cancelled with its X.
  await expenseRows.nth(1).locator('[data-remove-expense]').click();
  await expect(expenseRows).toHaveCount(2);
  await expect(page.locator('#expenseList')).not.toContainText('Fahad');
  await expect(page.locator('#expenseList')).toContainText('Lunch');
  await expect(page.locator('#expenseList')).toContainText('Breakfast');
  // Expenses and the money that depends on them move...
  await expect(page.locator('#summaryCards')).toContainText('UGX 20,000');
  await expect(page.locator('#expectedCashBig')).toHaveText('UGX 2,803,500');
  // ...and nothing else does.
  await expect(compactStock.locator('[data-stock-field="issued"]')).toHaveValue('101');
  await expect(compactStock.locator('[data-stock-field="previous"]')).toHaveValue('52');
  await expect(compactStock.locator('[data-stock-field="returned"]')).toHaveValue('147');
  await expect(compactStock.locator('[data-stock-sold]')).toHaveText('3 cartons');
  await expect(compactCard.locator('.price-row').nth(0).locator('[data-card-field="quantity"]')).toHaveValue('1.5');
  await expect(compactCard.locator('.price-row').nth(1).locator('[data-card-field="unit_price_ugx"]')).toHaveValue('80000');
  await expect(napkinCard.locator('.price-row').nth(0).locator('[data-card-field="quantity"]')).toHaveValue('20');
  await expect(strawCard.locator('.price-row').nth(1).locator('[data-card-field="quantity"]')).toHaveValue('0.5');
  await expect(page.locator('.vstock-row')).toHaveCount(3);
  await expect(page.locator('#mobileFromSales')).toHaveValue('500000');
  await expect(page.locator('#actualCash')).toHaveValue('2823500');
  await expect(page.locator('#summaryCards')).toContainText('UGX 2,823,500');   // total sales unchanged
  expect(stockWrites).toEqual([]);
  // The first and the last expense go the same way.
  await expenseRows.nth(0).locator('[data-remove-expense]').click();
  await expect(expenseRows).toHaveCount(1);
  await expect(page.locator('#expectedCashBig')).toHaveText('UGX 2,813,500');
  await expenseRows.nth(0).locator('[data-remove-expense]').click();
  await expect(expenseRows).toHaveCount(0);
  await expect(page.locator('#expenseList')).toContainText('No expenses recorded for this date');
  await expect(page.locator('#expectedCashBig')).toHaveText('UGX 2,823,500');
  await expect(page.locator('#mobileFromSales')).toHaveValue('500000');
  await expect(page.locator('#actualCash')).toHaveValue('2823500');
  await expect(page.locator('.vstock-row')).toHaveCount(3);
  expect(dialogs).toEqual([]);


  // ---- TEST F: a refused Close Day rolls everything back -----------------
  // Nothing typed in Vehicle stock or on the cards has reached the database.
  const beforeClose = await dayJson(page, BUSINESS_DATE);
  expect(beforeClose.day.status).toBe('draft');
  expect(beforeClose.stock.find(r => r.product_name === 'Compact Standard').closing).toBeNull();
  expect(beforeClose.totals.total_sales_ugx).toBe(0);
  expect(beforeClose.product_cards).toEqual([]);

  // Napkins is priced for 25 cartons but the sheet now says 20 were sold.
  await napkinStock.locator('[data-stock-field="returned"]').fill('10');
  await expect(napkinStock.locator('[data-stock-sold]')).toHaveText('20 cartons');
  await Promise.all([
    page.waitForResponse(r => r.url().endsWith('/close') && r.request().method() === 'POST'),
    page.locator('#closeDay').click(),
  ]);
  await expect(page.locator('#message')).toContainText('Napkins Standard');
  await expect(page.locator('#message')).toContainText('were sold');
  await expect(page.locator('#statusPill')).toHaveText('DRAFT');
  const afterRefusal = await dayJson(page, BUSINESS_DATE);
  expect(afterRefusal.day.status).toBe('draft');
  expect(afterRefusal.stock.find(r => r.product_name === 'Compact Standard').closing).toBeNull();
  expect(afterRefusal.totals.total_sales_ugx).toBe(0);
  expect(afterRefusal.product_cards).toEqual([]);
  // Every typed figure is still on screen to correct.
  await expect(compactStock.locator('[data-stock-field="issued"]')).toHaveValue('101');
  await expect(compactStock.locator('[data-stock-field="returned"]')).toHaveValue('147');
  await expect(compactCard.locator('.price-row').nth(1).locator('[data-card-field="quantity"]')).toHaveValue('1.5');
  await expect(page.locator('.vstock-row')).toHaveCount(3);

  // ---- TEST E: Close Day saves everything in one transaction -------------
  await napkinStock.locator('[data-stock-field="returned"]').fill('5');
  await expect(napkinStock.locator('[data-stock-sold]')).toHaveText('25 cartons');
  await page.locator('#actualCash').fill('2823500');
  await Promise.all([
    page.waitForResponse(r => r.url().endsWith(`/api/sales/days/${BUSINESS_DATE}`) && r.request().method() === 'PATCH'),
    page.locator('#saveCounts').click(),
  ]);
  await expect(page.locator('#message')).toContainText('Counted money saved');
  await Promise.all([
    page.waitForResponse(r => r.url().endsWith('/close') && r.request().method() === 'POST'),
    page.locator('#closeDay').click(),
  ]);
  await expect(page.locator('#statusPill')).toHaveText('CLOSED');
  await expect(page.locator('#message')).toContainText('Day closed');

  const closedDay = await dayJson(page, BUSINESS_DATE);
  expect(closedDay.day.status).toBe('closed');
  expect(closedDay.totals.total_sales_ugx).toBe(2823500);
  const closedCompact = closedDay.stock.find(r => r.product_name === 'Compact Standard');
  expect(closedCompact.closing.cartons).toBe(147);
  expect(closedCompact.total_stock.cartons).toBe(150);
  const closedCards = closedDay.product_cards;
  expect(closedCards).toHaveLength(3);
  const compactRows = closedCards.find(c => c.product_id === closedCompact.product_id).rows;
  expect(compactRows.map(r => [r.quantity, r.unit, r.unit_price_ugx])).toEqual([
    [1.5, 'carton', 79000], [1.5, 'carton', 80000]]);
  expect(compactRows.map(r => r.base_unit_qty)).toEqual([150, 150]);

  // Reloading reproduces exactly what was submitted.
  await openDay(page, BUSINESS_DATE);
  await expect(page.locator('#statusPill')).toHaveText('CLOSED');
  const reloadedCompact = stockRow(page, 'Compact Standard');
  await expect(reloadedCompact.locator('[data-stock-field="issued"]')).toHaveValue('101');
  await expect(reloadedCompact.locator('[data-stock-field="returned"]')).toHaveValue('147');
  await expect(reloadedCompact.locator('[data-stock-sold]')).toHaveText('3 cartons');
  const reloadedCard = salesCard(page, 'Compact Standard');
  await expect(reloadedCard.locator('.price-row').nth(0).locator('[data-card-field="quantity"]')).toHaveValue('1.5');
  await expect(reloadedCard.locator('.price-row').nth(1).locator('[data-card-field="quantity"]')).toHaveValue('1.5');
  await expect(page.locator('#summaryCards')).toContainText('UGX 2,823,500');
  // A closed day is read-only, and still has no Save button anywhere.
  await expect(reloadedCompact.locator('[data-stock-field="returned"]')).toBeDisabled();
  await expect(page.locator('#addStockProduct')).toBeDisabled();
  await expect(reloadedCard.locator('[data-card-field="quantity"]').first()).toBeDisabled();
  await expect(page.locator('[data-card-save]')).toHaveCount(0);
  // Still nothing but the one close request wrote the sheet.
  expect(stockWrites).toEqual([]);
  expect(cardWrites).toEqual([]);


  // ---- TEST C: descriptions are remembered for later days ----------------
  await openDay(page, NEXT_DATE);
  await expect(page.locator('#statusPill')).toHaveText('DRAFT');
  await showExpenseForm(page);
  // Typing "L" offers yesterday's "Lunch" as a tappable suggestion.
  await page.locator('#expenseDescription').fill('L');
  const chips = page.locator('#expenseMatches .suggest-chip');
  await expect(chips.filter({ hasText: 'Lunch' })).toHaveCount(1);
  await chips.filter({ hasText: 'Lunch' }).click();
  await expect(page.locator('#expenseDescription')).toHaveValue('Lunch');
  await expect(chips).toHaveCount(0);
  // Capitals never create a second logical suggestion.
  await expect(page.locator('#expenseSuggestions option[value="Lunch"]')).toHaveCount(1);
  await expect(page.locator('#expenseSuggestions option[value="lunch"]')).toHaveCount(0);
  await expect(page.locator('#expenseSuggestions option[value="LUNCH"]')).toHaveCount(0);
  await page.locator('#expenseDescription').fill('lunch');
  await expect(chips).toHaveCount(0);
  // A brand-new description is still free text, and is remembered afterwards.
  await page.locator('#expenseDescription').fill('Transport');
  await expect(chips).toHaveCount(0);
  await page.locator('#expenseAmount').fill('3000');
  await Promise.all([
    page.waitForResponse(r => r.url().includes('/expenses') && r.request().method() === 'POST'),
    page.locator('#addExpense').click(),
  ]);
  await page.locator('#expenseDescription').fill('T');
  await expect(chips.filter({ hasText: 'Transport' })).toHaveCount(1);
  await page.locator('#expenseDescription').fill('');

  // ---- TEST J: today's work never rewrites yesterday ---------------------
  await openDay(page, BUSINESS_DATE);
  expect(await stockOf(page, BUSINESS_DATE, 'Compact Standard')).toMatchObject({ total_stock: { cartons: 150 } });
  const yesterday = await dayJson(page, BUSINESS_DATE);
  expect(yesterday.totals.total_sales_ugx).toBe(2823500);
  expect(yesterday.stock.find(r => r.product_name === 'Compact Standard').closing.cartons).toBe(147);
  expect(yesterday.expenses).toEqual([]);
  const tomorrow = await dayJson(page, NEXT_DATE);
  expect(tomorrow.expenses.map(e => e.description)).toEqual(['Transport']);

  // ---- TEST K: mobile 375 x 812 ------------------------------------------
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  await expect(page.locator('[data-card-save]')).toHaveCount(0);
  await expect(page.locator('.sales-card [data-card-field="unit"]')).toHaveCount(0);
  await expect(page.locator('.sales-card select')).toHaveCount(0);
  // Every control - including the X buttons and the suggestion chips - is a
  // comfortable touch target (at least 36px tall).
  await openDay(page, NEXT_DATE);
  await showExpenseForm(page);
  await page.locator('#expenseDescription').fill('T');
  const tooShort = await page.evaluate(() => {
    const targets = [...document.querySelectorAll('#dailyView input, #dailyView select, #dailyView button')];
    return targets.filter((el) => {
      const box = el.getBoundingClientRect();
      return box.width > 0 && box.height > 0 && box.height < 36;
    }).map((el) => el.id || el.getAttribute('data-remove-expense') || el.tagName);
  });
  expect(tooShort).toEqual([]);
  const chipBox = await page.locator('#expenseMatches .suggest-chip').first().boundingBox();
  expect(chipBox.height).toBeGreaterThanOrEqual(36);
  const overflowAgain = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflowAgain).toBeLessThanOrEqual(0);
});
