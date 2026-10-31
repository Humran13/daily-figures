const { test, expect } = require('@playwright/test');

async function loginAsSalesOperator(page) {
  await page.goto('/');
  await page.locator('[data-section="sales"]').click();
  await page.locator('#usernameInput').fill('browser-sales');
  await page.locator('#passwordInput').fill('BrowserPass123');
  await page.locator('#loginBtn').click();
  await expect(page).toHaveURL(/\/sales\.html/);
  await expect(page.getByRole('heading', { name: 'Daily accountability' })).toBeVisible();
}

// Deterministic date change: the page reacts to the browser's change event.
async function chooseDate(page, value) {
  await page.locator('#businessDate').evaluate((el, date) => {
    el.value = date;
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }, value);
}

test('Sales operator opens today automatically and reproduces the 30/10/2026 paper sheet', async ({ page }) => {
  await loginAsSalesOperator(page);

  // ---- Operator first load: no selector, no "Open day", returns visible ----
  await expect(page.locator('#statusPill')).toHaveText('DRAFT');
  await expect(page.locator('#profileWrap')).toBeHidden();
  await expect(page.locator('#loadDay')).toBeHidden();
  const today = await page.evaluate(async () => (await (await fetch('/api/session')).json()).business_today);
  await expect(page.locator('#businessDate')).toHaveValue(today);
  await expect(page.locator('[data-return-qty]').first()).toBeVisible();
  await expect(page.locator('#stockList')).toContainText('Previous Return');
  await expect(page.locator('#stockList')).toContainText('Stock Added');
  await expect(page.locator('#stockList')).toContainText('Total Stock');
  await expect(page.locator('#stockList')).toContainText('Return Today');
  await expect(page.locator('#saveReturns')).toBeVisible();
  // Previous Return / Stock Added / Total Stock / Sold are read-only text:
  // the only numeric boxes in the stock list are the Return Today inputs.
  const stockRows = await page.locator('[data-stock-row]').count();
  expect(stockRows).toBe(2);
  await expect(page.locator('#stockList input[type="number"]')).toHaveCount(stockRows);
  // No customer and no Walk-in anywhere on the driver's daily screen.
  await expect(page.locator('#saleCustomer')).toHaveCount(0);
  await expect(page.getByText('Walk-in', { exact: false })).toHaveCount(0);
  await expect(page.getByText('No customer is needed', { exact: false })).toBeVisible();
  // A product with no stock, no load, no return and no sale never clutters the list.
  await expect(page.locator('#stockList')).not.toContainText('Straws');
  const firstLoadOverflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(firstLoadOverflow).toBeLessThanOrEqual(0);

  // ---- A historical date is shown, never silently created ----
  await chooseDate(page, '2026-10-30');
  await expect(page.locator('#statusPill')).toHaveText('NOT OPENED');
  await expect(page.locator('#startDate')).toBeVisible();
  await expect(page.locator('[data-return-qty]')).toHaveCount(0);
  await Promise.all([
    page.waitForResponse((r) => r.url().endsWith('/api/sales/days') && r.request().method() === 'POST'),
    page.locator('#startDate').click(),
  ]);
  await expect(page.locator('#statusPill')).toHaveText('DRAFT');
  await expect(page.locator('#startDate')).toBeHidden();

  // ---- 1. Stock & returns: 50 + 100 = 150, return 40 -> 110 sold ----
  const compactStock = page.locator('[data-stock-row]').filter({ hasText: 'Compact Standard' });
  const napkinStock = page.locator('[data-stock-row]').filter({ hasText: 'Napkins Standard' });
  await expect(compactStock).toContainText('Previous Return');
  await expect(compactStock).toContainText('50 cartons');
  await expect(compactStock).toContainText('100 cartons');
  await expect(compactStock).toContainText('150 cartons');
  await expect(napkinStock).toContainText('10 cartons');
  await expect(napkinStock).toContainText('20 cartons');
  await expect(napkinStock).toContainText('30 cartons');
  await compactStock.locator('[data-return-qty]').fill('40');
  await expect(compactStock.locator('[data-sold-label]')).toHaveText('110 cartons');
  await napkinStock.locator('[data-return-qty]').fill('5');
  await expect(napkinStock.locator('[data-sold-label]')).toHaveText('25 cartons');
  // One Save Returns button saves every product.
  await Promise.all([
    page.waitForResponse((r) => r.url().includes('/closing-stock') && r.request().method() === 'PUT'),
    page.locator('#saveReturns').click(),
  ]);
  await expect(page.locator('#message')).toContainText('Returns saved for 2 products');
  await expect(compactStock.locator('[data-sold-label]')).toHaveText('110 cartons');
  await expect(napkinStock.locator('[data-sold-label]')).toHaveText('25 cartons');

  // ---- 2. Sales value: prices are grouped under the sold quantity ----
  const compactSales = page.locator('[data-sale-group]').filter({ hasText: 'Compact Standard' });
  const napkinSales = page.locator('[data-sale-group]').filter({ hasText: 'Napkins Standard' });
  await expect(compactSales).toContainText('Sold from stock:');
  await expect(compactSales).toContainText('110 cartons');
  await expect(napkinSales).toContainText('25 cartons');

  async function addPrice(group, quantity, price, expectedValue) {
    await group.locator('[data-price-qty]').fill(String(quantity));
    await group.locator('[data-price-unit]').selectOption('carton');
    await group.locator('[data-price-price]').fill(String(price));
    if (expectedValue) await expect(group.locator('[data-price-preview]')).toHaveText(expectedValue);
    await Promise.all([
      page.waitForResponse((r) => r.url().endsWith('/api/sales/invoices') && r.request().method() === 'POST'),
      group.locator('[data-add-price]').click(),
    ]);
    await expect(page.locator('#message')).toContainText('Selling price saved');
  }

  await addPrice(compactSales, 100, 79000, 'UGX 7,900,000');
  await addPrice(compactSales, 10, 80000, 'UGX 800,000');
  await expect(compactSales).toContainText('100 cartons × UGX 79,000');
  await expect(compactSales).toContainText('10 cartons × UGX 80,000');
  await expect(compactSales).toContainText('✓ Balanced');
  await addPrice(napkinSales, 20, 103000, 'UGX 2,060,000');
  await addPrice(napkinSales, 5, 104000, 'UGX 520,000');
  await expect(napkinSales).toContainText('✓ Balanced');
  await expect(page.locator('#totalSalesBig')).toHaveText('UGX 11,280,000');

  // ---- 3. Expenses: 10,000 + 20,000 + 10,000 = 40,000 ----
  for (const [description, amount] of [['Lunch', 10000], ['Fahad', 20000], ['Breakfast', 10000]]) {
    await page.locator('#expenseDescription').fill(description);
    await page.locator('#expenseAmount').fill(String(amount));
    await Promise.all([
      page.waitForResponse((r) => r.url().endsWith('/api/sales/expenses') && r.request().method() === 'POST'),
      page.locator('#addExpense').click(),
    ]);
    await expect(page.locator('#message')).toContainText('Expense recorded');
  }
  await expect(page.locator('#expenseList')).toContainText('Lunch');
  await expect(page.locator('#expenseList')).toContainText('Fahad');
  await expect(page.locator('#expenseList')).toContainText('Breakfast');
  await expect(page.locator('#totalExpensesBig')).toHaveText('UGX 40,000');

  // ---- 4. Balance: all cash, no debtors, no Mobile Money, opening 0 ----
  await expect(page.locator('#netSalesBig')).toHaveText('UGX 11,240,000');
  await expect(page.locator('#expectedCashBig')).toHaveText('UGX 11,240,000');
  await expect(page.locator('#expectedMobileBig')).toHaveText('UGX 0');
  await expect(page.locator('#cashEquation')).toContainText('Total Sales');
  await expect(page.locator('#cashEquation')).toContainText('Cash from today');
  await expect(page.locator('#cashEquation')).toContainText('Less: Cash expenses');
  await expect(page.locator('#cashEquation')).not.toContainText('Less: Debtors');
  await expect(page.locator('#cashEquation')).not.toContainText('Less: Mobile Money');

  // Counted money matches the paper total.
  await page.locator('#actualCash').fill('11240000');
  await page.locator('#actualMobile').fill('0');
  await Promise.all([
    page.waitForResponse((r) => r.url().endsWith('/api/sales/days/2026-10-30') && r.request().method() === 'PATCH'),
    page.locator('#saveCounts').click(),
  ]);
  await expect(page.locator('#message')).toContainText('Counted money saved');
  await expect(page.locator('#cashDifferenceBig')).toHaveText('UGX 0');
  await expect(page.locator('#mobileDifferenceBig')).toHaveText('UGX 0');

  // ---- 5. Close day ----
  await page.locator('#closeDay').click();
  await expect(page.locator('#statusPill')).toHaveText('CLOSED');
  await expect(page.locator('#saveReturns')).toBeDisabled();

  // Ownership is still enforced for a normal Operator.
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
    }).map((el) => el.id || el.getAttribute('data-add-price') || el.tagName);
  });
  expect(tooShort).toEqual([]);
});
