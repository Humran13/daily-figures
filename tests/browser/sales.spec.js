const { test, expect } = require('@playwright/test');

test('Sales operator balances a day alone with one card per product', async ({ page }) => {
  await page.goto('/');
  await page.locator('[data-section="sales"]').click();
  await page.locator('#usernameInput').fill('browser-sales');
  await page.locator('#passwordInput').fill('BrowserPass123');
  await page.locator('#loginBtn').click();
  await expect(page).toHaveURL(/\/sales\.html/);
  await expect(page.getByRole('heading', { name: 'Daily accountability' })).toBeVisible();
  // Wait for the page's own first load so the controls are wired before use.
  await expect(page.locator('#statusPill')).not.toHaveText('Not loaded');

  await page.locator('#businessDate').fill('2026-10-30');
  await Promise.all([
    page.waitForResponse(r => r.url().endsWith('/api/sales/days') && r.request().method() === 'POST'),
    page.locator('#loadDay').click(),
  ]);
  await expect(page.locator('#statusPill')).toHaveText('DRAFT');

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

  // Step 1 — starting stock is the salesperson's own entry: on the day the
  // vehicle balance starts they correct it in place, with no approval.
  await expect(page.locator('#stockList')).toContainText('Previous Return');
  await expect(page.locator('#stockList')).toContainText('Return Today');
  await expect(page.locator('#openingNotice')).toBeHidden();
  const startInput = page.locator('.stock-row')
    .filter({ hasText: 'Compact Standard' }).locator('[data-open-qty]');
  await expect(startInput).toHaveAttribute('placeholder', '50 cartons');
  await startInput.fill('50');
  await Promise.all([
    page.waitForResponse(r => r.url().endsWith('/opening-stock') && r.request().method() === 'PUT'),
    page.locator('#saveOpening').click(),
  ]);
  await expect(page.locator('#message')).toContainText('Starting stock saved');
  await expect(page.locator('#stockList')).toContainText('50 cartons');
  // A normal sale needs no customer and no per-line payment method.
  await expect(page.getByText('No customer is needed', { exact: false })).toBeVisible();
  await expect(page.locator('#saleCustomer')).toHaveCount(0);
  await expect(page.getByText('Walk-in', { exact: false })).toHaveCount(0);

  // Step 1 — count today's return in one action: 40 cartons of Compact come
  // back, everything else is explicitly zero. That is the whole stock entry.
  const stockRowCount = await page.locator('.stock-row').count();
  for (let index = 0; index < stockRowCount; index += 1) {
    const row = page.locator('.stock-row').nth(index);
    const name = (await row.locator('.stock-head strong').first().textContent()).trim();
    await row.locator('[data-close-qty]').fill(name === 'Compact Standard' ? '40' : '0');
  }
  await Promise.all([
    page.waitForResponse(r => r.url().endsWith('/returns') && r.request().method() === 'PUT'),
    page.locator('#saveReturns').click(),
  ]);
  await expect(page.locator('#message')).toContainText('Returns today saved');

  // The equation itself is on screen: 50 carried + 0 added = 50 out,
  // 50 − 40 back = 10 sold. The sold quantity is never typed twice.
  const compactRow = page.locator('.stock-row').filter({ hasText: 'Compact Standard' });
  await expect(compactRow).toContainText('= 10 cartons sold');
  await expect(compactRow).toContainText('Previous Return');
  const soldRow = page.locator('.sold-row').filter({ hasText: 'Compact Standard' });
  await expect(soldRow).toContainText('10 cartons');
  await expect(soldRow).toContainText('Not priced yet');

  // Step 3 — one card per product. The card already knows 10 cartons were
  // sold, so a single row at the real price completes it.
  const card = page.locator('.sales-card').filter({ hasText: 'Compact Standard' });
  await expect(card).toContainText('10 cartons sold today');
  const cardStatus = page.locator(`#cardStatus-${await card.getAttribute('data-card-product')}`);
  await expect(cardStatus).toContainText('of 10 cartons sold');
  await card.locator('[data-card-field="quantity"]').fill('10');
  await card.locator('[data-card-field="unit_price_ugx"]').fill('79000');
  await expect(cardStatus).toContainText('matches the stock count');
  await expect(card.locator('.row-value').first()).toHaveText('UGX 790,000');
  await Promise.all([
    page.waitForResponse(r => r.url().includes('/products/') && r.url().endsWith('/sales') && r.request().method() === 'PUT'),
    card.locator('[data-card-save]').click(),
  ]);
  await expect(page.locator('#message')).toContainText('sales card saved');
  await expect(soldRow).toContainText('Fully priced');
  await expect(page.locator('#summaryCards')).toContainText('UGX 790,000');

  // A card whose rows do not add up to the counted sales is refused, and the
  // refusal names the quantity that has to change.
  await card.locator('[data-card-field="quantity"]').fill('12');
  await expect(cardStatus).toContainText('but only 10 cartons were sold');
  await Promise.all([
    page.waitForResponse(r => r.url().includes('/products/') && r.url().endsWith('/sales') && r.request().method() === 'PUT'),
    card.locator('[data-card-save]').click(),
  ]);
  await expect(page.locator('#message')).toContainText('but only 10 cartons were sold');
  await card.locator('[data-card-field="quantity"]').fill('10');

  // Step 5 — Mobile Money is recorded once for the whole day, never per row.
  await page.locator('#mobileFromSales').fill('200000');
  await Promise.all([
    page.waitForResponse(r => r.url().endsWith('/api/sales/days/2026-10-30') && r.request().method() === 'PATCH'),
    page.locator('#saveMobileSplit').click(),
  ]);
  await expect(page.locator('#expectedCashBig')).toHaveText('UGX 590,000');
  await expect(page.locator('#expectedMobileBig')).toHaveText('UGX 200,000');

  // Step 6 — expenses are a normal daily action but the form stays out of the
  // way until it is needed.
  await page.locator('[data-toggle-form="expenseForm"]').click();
  await page.locator('#expenseDescription').fill('Lunch');
  await page.locator('#expenseAmount').fill('10000');
  await Promise.all([
    page.waitForResponse(r => r.url().includes('/expenses') && r.request().method() === 'POST'),
    page.locator('#addExpense').click(),
  ]);
  await expect(page.locator('#expectedCashBig')).toHaveText('UGX 580,000');

  // Step 7 — the salesperson counts their own money and closes the day with
  // no Manager and no Super Administrator involved.
  await page.locator('#actualCash').fill('580000');
  await page.locator('#actualMobile').fill('200000');
  await Promise.all([
    page.waitForResponse(r => r.url().endsWith('/api/sales/days/2026-10-30') && r.request().method() === 'PATCH'),
    page.locator('#saveCounts').click(),
  ]);
  await expect(page.locator('#message')).toContainText('Counted money saved');
  await expect(page.locator('#cashDifferenceBig')).toHaveText('UGX 0');
  await expect(page.locator('#mobileDifferenceBig')).toHaveText('UGX 0');
  await Promise.all([
    page.waitForResponse(r => r.url().endsWith('/close') && r.request().method() === 'POST'),
    page.locator('#closeDay').click(),
  ]);
  await expect(page.locator('#statusPill')).toHaveText('CLOSED');
  await expect(page.locator('#reopenDay')).toBeHidden();

  // A closed day is read-only: the counts and every entry form disappear.
  await expect(page.locator('[data-close-qty]')).toHaveCount(0);
  await expect(page.locator('#saveReturns')).toBeDisabled();
  await expect(page.locator('#saveOpening')).toBeDisabled();
  await expect(page.locator('#declareForm')).toBeHidden();
  await expect(page.locator('[data-card-save]').first()).toBeDisabled();

  const tamperStatus = await page.evaluate(async () => {
    const response = await fetch('/api/sales/days/2026-10-30?sales_profile_id=999999');
    return response.status;
  });
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
  // re-declared, nothing is approved and the salesperson owns it alone.
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
