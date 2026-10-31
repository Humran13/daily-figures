const { test, expect } = require('@playwright/test');

test('Sales operator completes a mobile daily workflow with ownership enforced', async ({ page }) => {
  await page.goto('/');
  await page.locator('[data-section="sales"]').click();
  await page.locator('#usernameInput').fill('browser-sales');
  await page.locator('#passwordInput').fill('BrowserPass123');
  await page.locator('#loginBtn').click();
  await expect(page).toHaveURL(/\/sales\.html/);
  await expect(page.getByRole('heading', { name: 'Daily accountability' })).toBeVisible();

  await page.locator('#businessDate').fill('2026-10-30');
  await page.locator('#loadDay').click();
  await expect(page.locator('#stockList')).toContainText('Compact Standard');
  await expect(page.locator('#baselineWarning')).toBeHidden();

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

  await page.locator('#saleProduct').selectOption({ label: 'Compact Standard' });
  await page.locator('#saleQty').fill('10');
  await page.locator('#saleUnit').selectOption('carton');
  await page.locator('#salePrice').fill('79000');
  await page.locator('#saleMethod').selectOption('cash');
  await page.locator('#addSale').click();
  await expect(page.locator('#summaryCards')).toContainText('UGX 790,000');

  await page.locator('#expenseDescription').fill('Lunch');
  await page.locator('#expenseAmount').fill('10000');
  await page.locator('#addExpense').click();
  await expect(page.locator('#summaryCards')).toContainText('UGX 780,000');

  await page.locator('#actualCash').fill('780000');
  await page.locator('#saveCounts').click();
  await page.locator('#closeDay').click();
  await expect(page.locator('#statusPill')).toHaveText('CLOSED');

  const tamperStatus = await page.evaluate(async () => {
    const response = await fetch('/api/sales/days/2026-10-30?sales_profile_id=999999');
    return response.status;
  });
  expect(tamperStatus).toBe(403);

  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
});
