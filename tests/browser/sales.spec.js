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

  const stockRows = page.locator('.stock-row');
  await expect(stockRows).toHaveCount(2);
  const compactStock = stockRows.filter({ hasText: 'Compact Standard' });
  const napkinStock = stockRows.filter({ hasText: 'Napkins Standard' });
  for (const label of ['Previous Return', 'Stock Added', 'Total Stock', 'Return Today', 'Sold']) {
    await expect(compactStock).toContainText(label);
  }
  // Previous Return and Stock Added are read-only facts: 50 carried, 100 loaded.
  await expect(compactStock).toContainText('50 cartons');
  await expect(compactStock).toContainText('100 cartons');
  await expect(compactStock).toContainText('150 cartons');
  await expect(napkinStock).toContainText('10 cartons');
  await expect(napkinStock).toContainText('20 cartons');
  await expect(napkinStock).toContainText('30 cartons');
  // Starting stock is the salesperson's own figure and needs no approval.
  await expect(compactStock.locator('[data-open-qty]')).toHaveAttribute('placeholder', '50 cartons');
  await expect(page.locator('#openingNotice')).toBeHidden();
  // A normal sale needs no customer and no per-line payment method.
  await expect(page.getByText('No customer is needed', { exact: false })).toBeVisible();
  await expect(page.locator('#saleCustomer')).toHaveCount(0);
  await expect(page.getByText('Walk-in', { exact: false })).toHaveCount(0);

  // Sold is previewed as the return is typed: 150 − 40 = 110, 30 − 5 = 25.
  await compactStock.locator('[data-close-qty]').fill('40');
  await expect(compactStock.locator('[data-sold-datum]')).toHaveText('110 cartons');
  await napkinStock.locator('[data-close-qty]').fill('5');
  await expect(napkinStock.locator('[data-sold-datum]')).toHaveText('25 cartons');

  await Promise.all([
    page.waitForResponse(r => r.url().endsWith('/returns') && r.request().method() === 'PUT'),
    page.locator('#saveReturns').click(),
  ]);
  await expect(page.locator('#message')).toContainText('Returns today saved');
  await expect(compactStock.locator('[data-sold-datum]')).toHaveText('110 cartons');
  await expect(napkinStock.locator('[data-sold-datum]')).toHaveText('25 cartons');
  await expect(compactStock).toContainText('= 110 cartons sold');

  // Step 2 takes the sold quantity straight from the stock count.
  const soldRows = page.locator('.sold-row');
  await expect(soldRows.filter({ hasText: 'Compact Standard' })).toContainText('110 cartons');
  await expect(soldRows.filter({ hasText: 'Napkins Standard' })).toContainText('25 cartons');

  // Step 3 — one card per product; every price row adds up to Sold.
  const cards = page.locator('.sales-card');
  await expect(cards).toHaveCount(2);
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

  // A closed day is read-only: no counts and no entry forms.
  await expect(page.locator('[data-close-qty]')).toHaveCount(0);
  await expect(page.locator('#saveReturns')).toBeDisabled();
  await expect(page.locator('#saveOpening')).toBeDisabled();
  await expect(page.locator('#declareForm')).toBeHidden();
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

  // The next day starts from the counted return automatically — nothing is
  // re-declared and no Manager is involved.
  await page.locator('#businessDate').fill('2026-10-31');
  await Promise.all([
    page.waitForResponse(r => r.url().endsWith('/api/sales/days') && r.request().method() === 'POST'),
    page.locator('#loadDay').click(),
  ]);
  await expect(page.locator('#statusPill')).toHaveText('DRAFT');
  const nextCompact = page.locator('.stock-row').filter({ hasText: 'Compact Standard' });
  await expect(nextCompact).toContainText('40 cartons');
  await expect(page.locator('[data-open-qty]')).toHaveCount(0);
  await expect(page.locator('#openingNotice')).toBeHidden();
});
