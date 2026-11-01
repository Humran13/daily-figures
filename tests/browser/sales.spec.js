const { test, expect } = require('@playwright/test');

const BUSINESS_DATE = '2026-10-30';

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

test('Salesperson reproduces the 30/10/2026 paper sheet alone', async ({ page }) => {
  await loginAsSalesOperator(page);

  // Today's day is opened for the salesperson automatically and there is no
  // "who am I" selector: a driver only thinks about their own day.
  await expect(page.locator('#profileWrap')).toBeHidden();
  const today = await page.evaluate(async () => (await (await fetch('/api/session')).json()).business_today);
  await expect(page.locator('#businessDate')).toHaveValue(today);

  // The seven steps read in the order a salesperson actually works.
  const headings = await page.locator('#dailyView .card h2').allTextContents();
  expect(headings.map(h => h.replace(/^\s*\d+\s*/, '').trim())).toEqual([
    'Vehicle stock',
    'Sold today',
    'Sales cards',
    'Total sales today',
    'Mobile Money',
    'Expenses and money movement',
    'Cash at hand',
  ]);

  // ---- 30/10/2026: Compact 50 + 100 = 150, Napkins 10 + 20 = 30 ----
  await page.locator('#businessDate').fill(BUSINESS_DATE);
  await Promise.all([
    page.waitForResponse(r => r.url().endsWith('/api/sales/days') && r.request().method() === 'POST'),
    page.locator('#loadDay').click(),
  ]);
  await expect(page.locator('#statusPill')).toHaveText('DRAFT');

  // ---- Step 1: Vehicle stock is the salesperson's own editable sheet ----
  const stockRows = page.locator('.vstock-row');
  await expect(stockRows).toHaveCount(2);
  const compactStock = stockRows.filter({ hasText: 'Compact Standard' });
  const napkinStock = stockRows.filter({ hasText: 'Napkins Standard' });
  for (const label of ['Previous Return', 'Given Stock / Issued Today', 'Total Stock', 'Return Today', 'Sold Today']) {
    await expect(compactStock).toContainText(label);
  }
  // All four stock figures are editable inputs, prefilled from the system.
  await expect(compactStock.locator('[data-stock-field="previous"]')).toHaveValue('50');
  await expect(compactStock.locator('[data-stock-field="issued"]')).toHaveValue('100');
  await expect(compactStock.locator('[data-stock-field="total"]')).toHaveValue('150');
  await expect(compactStock.locator('[data-stock-field="returned"]')).toHaveValue('');
  await expect(napkinStock.locator('[data-stock-field="previous"]')).toHaveValue('10');
  await expect(napkinStock.locator('[data-stock-field="issued"]')).toHaveValue('20');
  await expect(napkinStock.locator('[data-stock-field="total"]')).toHaveValue('30');
  await expect(page.locator('#openingNotice')).toBeHidden();
  // A normal sale needs no customer and no per-line payment method.
  await expect(page.getByText('No customer is needed', { exact: false })).toBeVisible();
  await expect(page.locator('#saleCustomer')).toHaveCount(0);
  await expect(page.getByText('Walk-in', { exact: false })).toHaveCount(0);

  // The wrong old design is gone: no separate starting-stock form, no second
  // disconnected stock entry underneath the cards.
  await expect(page.locator('#declareForm')).toHaveCount(0);
  await expect(page.locator('#saveOpening')).toHaveCount(0);
  await expect(page.locator('#saveReturns')).toHaveCount(0);

  // Acceptance 1: Previous Return is correctable, Total Stock follows it and
  // can then be corrected by hand: 52 + 100 = 152 → 150, less 40 = 110 sold.
  await compactStock.locator('[data-stock-field="previous"]').fill('52');
  await expect(compactStock.locator('[data-stock-field="total"]')).toHaveValue('152');
  await compactStock.locator('[data-stock-field="total"]').fill('150');
  // Once Total Stock is corrected by hand the formula stops fighting it.
  await compactStock.locator('[data-stock-field="issued"]').fill('90');
  await expect(compactStock.locator('[data-stock-field="total"]')).toHaveValue('150');
  await compactStock.locator('[data-stock-field="issued"]').fill('100');
  await expect(compactStock.locator('[data-stock-field="total"]')).toHaveValue('150');
  await compactStock.locator('[data-stock-field="returned"]').fill('40');
  await expect(compactStock.locator('[data-stock-sold]')).toHaveText('110 cartons');
  // Acceptance 2: 10 + 20 = 30, 5 back, 25 sold.
  await napkinStock.locator('[data-stock-field="returned"]').fill('5');
  await expect(napkinStock.locator('[data-stock-sold]')).toHaveText('25 cartons');

  // An impossible count is shown and blocked, never silently saved.
  await compactStock.locator('[data-stock-field="returned"]').fill('151');
  await expect(compactStock.locator('[data-stock-error]')).toHaveText('Return Today cannot be greater than Total Stock.');
  await compactStock.locator('[data-stock-field="returned"]').fill('40');

  // Acceptance 3: the Product dropdown is the product catalogue, not a list in
  // the code, and a product already on the sheet is not offered again.
  await page.locator('#addStockProduct').click();
  const picker = page.locator('[data-stock-pick]');
  await expect(picker.locator('option', { hasText: 'Straws' })).toHaveCount(1);
  const offerable = await picker.locator('option').allTextContents();
  expect(offerable).not.toContain('Compact Standard');
  expect(offerable).not.toContain('Napkins Standard');
  expect(offerable).toContain('Straws');
  await picker.selectOption({ label: 'Straws' });
  const strawStock = stockRows.filter({ hasText: 'Straws' });
  await expect(strawStock.locator('[data-stock-field="previous"]')).toHaveValue('0');
  await strawStock.locator('[data-stock-field="issued"]').fill('12');
  await expect(strawStock.locator('[data-stock-field="total"]')).toHaveValue('12');
  await strawStock.locator('[data-stock-field="returned"]').fill('2');
  await expect(strawStock.locator('[data-stock-sold]')).toHaveText('10 cartons');
  // Nothing else changes when another product's card changes.
  await expect(compactStock.locator('[data-stock-sold]')).toHaveText('110 cartons');
  // Straws all came back, so it contributes no sales to the rest of the day.
  await strawStock.locator('[data-stock-field="returned"]').fill('12');

  await Promise.all([
    page.waitForResponse(r => r.url().endsWith('/vehicle-stock') && r.request().method() === 'PUT'),
    page.locator('#saveVehicleStock').click(),
  ]);
  await expect(page.locator('#message')).toContainText('Vehicle stock saved');
  await expect(page.locator('.vstock-row')).toHaveCount(3);
  await expect(page.locator('.vstock-row').filter({ hasText: 'Compact Standard' })
    .locator('[data-stock-field="previous"]')).toHaveValue('52');
  await expect(page.locator('.vstock-row').filter({ hasText: 'Compact Standard' })
    .locator('[data-stock-field="returned"]')).toHaveValue('40');
  await expect(page.locator('.vstock-row').filter({ hasText: 'Straws' })
    .locator('[data-stock-field="issued"]')).toHaveValue('12');

  // Step 2 takes the sold quantity straight from the stock count.
  const soldRows = page.locator('.sold-row');
  await expect(soldRows).toHaveCount(3);
  await expect(soldRows.filter({ hasText: 'Compact Standard' })).toContainText('110 cartons');
  await expect(soldRows.filter({ hasText: 'Napkins Standard' })).toContainText('25 cartons');
  await expect(soldRows.filter({ hasText: 'Straws' })).toContainText('Nothing sold');

  // Step 3 — one card per product; every price row adds up to Sold.
  const cards = page.locator('.sales-card');
  await expect(cards).toHaveCount(3);
  const compactCard = cards.filter({ hasText: 'Compact Standard' });
  const napkinCard = cards.filter({ hasText: 'Napkins Standard' });

  async function fillPriceRow(card, index, quantity, price) {
    const row = card.locator('.price-row').nth(index);
    await row.locator('[data-card-field="quantity"]').fill(String(quantity));
    await row.locator('[data-card-field="unit"]').selectOption('carton');
    await row.locator('[data-card-field="unit_price_ugx"]').fill(String(price));
  }
  async function saveCard(card) {
    await Promise.all([
      page.waitForResponse(r => r.url().includes('/products/') && r.url().endsWith('/sales') && r.request().method() === 'PUT'),
      card.locator('[data-card-save]').click(),
    ]);
  }

  // Compact: 100 × 79,000 and 10 × 80,000 = UGX 8,700,000 for 110 cartons.
  await fillPriceRow(compactCard, 0, 100, 79000);
  await compactCard.locator('[data-card-add]').click();
  await fillPriceRow(compactCard, 1, 10, 80000);
  await expect(compactCard.locator('.card-status')).toContainText('matches the stock count');
  await expect(compactCard.locator('.card-status')).toContainText('UGX 8,700,000');
  await saveCard(compactCard);
  await expect(page.locator('#message')).toContainText('Compact Standard sales card saved');
  await expect(compactCard).toContainText('UGX 8,700,000 saved');
  await expect(compactCard.locator('.price-row')).toHaveCount(2);

  // Napkins: 20 × 103,000 and 5 × 104,000 = UGX 2,580,000 for 25 cartons.
  await fillPriceRow(napkinCard, 0, 20, 103000);
  await napkinCard.locator('[data-card-add]').click();
  await fillPriceRow(napkinCard, 1, 10, 104000);
  // A card that does not add up to Sold is refused with the exact difference.
  await saveCard(napkinCard);
  await expect(page.locator('#message')).toContainText('but only 25 cartons were sold');
  await fillPriceRow(napkinCard, 1, 5, 104000);
  await saveCard(napkinCard);
  await expect(page.locator('#message')).toContainText('Napkins Standard sales card saved');
  await expect(napkinCard).toContainText('UGX 2,580,000 saved');

  // Step 4 — Total Sales is the sum of both cards, never a retyped figure.
  await expect(page.locator('#summaryCards')).toContainText('UGX 11,280,000');
  await expect(page.locator('#tenderHint')).toContainText('UGX 11,280,000');
  // Mobile Money is nothing today, so it changes no figure.
  await expect(page.locator('#expectedMobileBig')).toHaveText('UGX 0');

  // Step 6 — expenses 10,000 + 20,000 + 10,000 = 40,000.
  await page.locator('[data-toggle-form="expenseForm"]').click();
  for (const [description, amount] of [['Lunch', 10000], ['Fahad', 20000], ['Breakfast', 10000]]) {
    await page.locator('#expenseDescription').fill(description);
    await page.locator('#expenseAmount').fill(String(amount));
    await Promise.all([
      page.waitForResponse(r => r.url().includes('/expenses') && r.request().method() === 'POST'),
      page.locator('#addExpense').click(),
    ]);
  }
  await expect(page.locator('#summaryCards')).toContainText('UGX 40,000');
  await expect(page.locator('#summaryCards')).toContainText('UGX 11,240,000');

  // Step 7 — Total Sales − Mobile Money − expenses = Cash at Hand.
  await expect(page.locator('#expectedCashBig')).toHaveText('UGX 11,240,000');
  await expect(page.locator('#cashEquation')).toHaveText(
    'Total Sales UGX 11,280,000 − Mobile Money UGX 0 − Cash expenses UGX 40,000 = Cash at Hand UGX 11,240,000.');

  await page.locator('#actualCash').fill('11240000');
  await page.locator('#actualMobile').fill('0');
  await Promise.all([
    page.waitForResponse(r => r.url().endsWith(`/api/sales/days/${BUSINESS_DATE}`) && r.request().method() === 'PATCH'),
    page.locator('#saveCounts').click(),
  ]);
  await expect(page.locator('#message')).toContainText('Counted money saved');
  await expect(page.locator('#cashDifferenceBig')).toHaveText('UGX 0');
  await expect(page.locator('#mobileDifferenceBig')).toHaveText('UGX 0');

  // The salesperson completes the reconciliation alone: no Manager, no approval.
  await Promise.all([
    page.waitForResponse(r => r.url().endsWith('/close') && r.request().method() === 'POST'),
    page.locator('#closeDay').click(),
  ]);
  await expect(page.locator('#statusPill')).toHaveText('CLOSED');
  await expect(page.locator('#reopenDay')).toBeHidden();

  // A closed day is read-only: the sheet is shown, nothing on it can change.
  await expect(page.locator('[data-stock-field="returned"]').first()).toBeDisabled();
  await expect(page.locator('[data-stock-field="previous"]').first()).toBeDisabled();
  await expect(page.locator('#saveVehicleStock')).toBeDisabled();
  await expect(page.locator('#addStockProduct')).toBeDisabled();
  await expect(page.locator('[data-card-save]').first()).toBeDisabled();

  const tamperStatus = await page.evaluate(
    async (date) => (await fetch(`/api/sales/days/${date}?sales_profile_id=999999`)).status, BUSINESS_DATE);
  expect(tamperStatus).toBe(403);

  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);

  // Touch-friendly primary controls at 375 x 812.
  const tooShort = await page.evaluate(() => {
    const targets = [...document.querySelectorAll('#dailyView input, #dailyView select, #dailyView button')];
    return targets.filter((el) => {
      const box = el.getBoundingClientRect();
      return box.width > 0 && box.height > 0 && box.height < 36;
    }).map((el) => el.id || el.tagName);
  });
  expect(tooShort).toEqual([]);

  // Acceptance 4 — the next day carries yesterday's counted return forward, and
  // today's correction never rewrites yesterday's closed record.
  await page.locator('#businessDate').fill('2026-10-31');
  await Promise.all([
    page.waitForResponse(r => r.url().endsWith('/api/sales/days') && r.request().method() === 'POST'),
    page.locator('#loadDay').click(),
  ]);
  await expect(page.locator('#statusPill')).toHaveText('DRAFT');
  const nextCompact = page.locator('.vstock-row').filter({ hasText: 'Compact Standard' });
  await expect(nextCompact.locator('[data-stock-field="previous"]')).toHaveValue('40');
  // The salesperson physically counts 42 on the vehicle and corrects today.
  await nextCompact.locator('[data-stock-field="previous"]').fill('42');
  await Promise.all([
    page.waitForResponse(r => r.url().endsWith('/vehicle-stock') && r.request().method() === 'PUT'),
    page.locator('#saveVehicleStock').click(),
  ]);
  await expect(page.locator('#message')).toContainText('Vehicle stock saved');

  // Reloading today shows today's own 42.
  await Promise.all([
    page.waitForResponse(r => r.url().endsWith('/api/sales/days') && r.request().method() === 'POST'),
    page.locator('#loadDay').click(),
  ]);
  await expect(page.locator('.vstock-row').filter({ hasText: 'Compact Standard' })
    .locator('[data-stock-field="previous"]')).toHaveValue('42');

  // Yesterday's closed day still says Previous 52 / Return 40: untouched.
  await page.locator('#businessDate').fill(BUSINESS_DATE);
  await Promise.all([
    page.waitForResponse(r => r.url().endsWith('/api/sales/days') && r.request().method() === 'POST'),
    page.locator('#loadDay').click(),
  ]);
  await expect(page.locator('#statusPill')).toHaveText('CLOSED');
  const closedCompact = page.locator('.vstock-row').filter({ hasText: 'Compact Standard' });
  await expect(closedCompact.locator('[data-stock-field="returned"]')).toHaveValue('40');
  await expect(closedCompact.locator('[data-stock-field="previous"]')).toHaveValue('52');
});
