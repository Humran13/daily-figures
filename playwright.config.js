const { defineConfig } = require('@playwright/test');

module.exports = defineConfig({
  testDir: './tests/browser',
  timeout: 30_000,
  use: {
    baseURL: process.env.SALES_TEST_BASE_URL || 'http://127.0.0.1:5055',
    viewport: { width: 375, height: 812 },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  reporter: [['line']],
});
