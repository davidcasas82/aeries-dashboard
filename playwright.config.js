const { defineConfig } = require('@playwright/test');

module.exports = defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  timeout: 30000,
  reporter: 'line',
  use: {
    baseURL: 'http://127.0.0.1:8765',
    viewport: { width: 390, height: 844 },
    screenshot: 'only-on-failure',
  },
  webServer: [
    {
      command: 'python3 .cursor/skills/verify-aeries-dashboard/helpers/fixture_api.py',
      url: 'http://127.0.0.1:8787/health',
      reuseExistingServer: false,
      timeout: 30000,
    },
    {
      command: 'python3 -m http.server 8765 --bind 127.0.0.1',
      url: 'http://127.0.0.1:8765/',
      reuseExistingServer: false,
      timeout: 30000,
    },
  ],
});
