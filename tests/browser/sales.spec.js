const { test, expect } = require('@playwright/test');

test('Sales operator completes a mobile daily workflow with ownership enforced', async ({ page }) => {
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
  await expect(page.locator('#stockList')).toContainText('Compact Standard');
  await expect(page.locator('#stockList')).toContainText('Previous Return');
  await expect(page.locator('#stockList')).toContainText('Return Today');
  await expect(page.locator('#baselineWarning')).toBeHidden();
  // A normal sale needs no customer and no per-line payment method.
  await expect(page.getByText('No customer is needed', { exact: false })).toBeVisible();
  await expect(page.locator('#saleCustomer')).toHaveCount(0);
  await expect(page.getByText('Walk-in', { exact: false })).toHaveCount(0);

  // The disposable migration contains several active products. Explicitly
  // count zero for products not carried, and 40 cartons for Compact.
  // Rows are re-rendered after every save, so walk them by index and read the
  // exact product name (substring filters would also match "Mambo Damage").
  const stockRowCount = await page.locator('.stock-row').count();
  for (let index = 0; index < stockRowCount; index += 1) {
    const row = page.locator('.stock-row').nth(index);
    const name = (await row.locator('.stock-head strong').first().textContent()).trim();
    await row.locator('[data-close-qty]').fill(name === 'Compact Standard' ? '40' : '0');
    await Promise.all([
      page.waitForResponse(r => r.url().includes('/closing-stock') && r.request().method() === 'PUT'),
      row.locator('[data-save-close]').click(),
    ]);
  }

  // The equation itself is on screen: 50 carried + 0 added = 50 out,
  // 50 − 40 back = 10 sold.
  const compactRow = page.locator('.stock-row').filter({ hasText: 'Compact Standard' });
  await expect(compactRow).toContainText('= 10 cartons sold');
  await expect(compactRow).toContainText('Previous Return');

  await page.locator('#saleProduct').selectOption({ label: 'Compact Standard' });
  await page.locator('#saleQty').fill('10');
  await page.locator('#saleUnit').selectOption('carton');
  await page.locator('#salePrice').fill('79000');
  await page.locator('#addSale').click();
  await expect(page.locator('#summaryCards')).toContainText('UGX 790,000');

  // Mobile Money is recorded once for the whole day, never per sale line.
  await page.locator('#mobileFromSales').fill('200000');
  await page.locator('#saveMobileSplit').click();
  await expect(page.locator('#expectedCashBig')).toHaveText('UGX 590,000');
  await expect(page.locator('#expectedMobileBig')).toHaveText('UGX 200,000');

  // Expenses are a normal daily action but the form stays out of the way
  // until it is needed.
  await page.locator('[data-toggle-form="expenseForm"]').click();
  await page.locator('#expenseDescription').fill('Lunch');
  await page.locator('#expenseAmount').fill('10000');
  await page.locator('#addExpense').click();
  await expect(page.locator('#expectedCashBig')).toHaveText('UGX 580,000');

  await page.locator('#actualCash').fill('580000');
  await page.locator('#actualMobile').fill('200000');
  await page.locator('#saveCounts').click();
  await expect(page.locator('#message')).toContainText('Counted money saved');
  await expect(page.locator('#cashDifferenceBig')).toHaveText('UGX 0');
  await expect(page.locator('#mobileDifferenceBig')).toHaveText('UGX 0');
  await page.locator('#closeDay').click();
  await expect(page.locator('#statusPill')).toHaveText('CLOSED');

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
});
