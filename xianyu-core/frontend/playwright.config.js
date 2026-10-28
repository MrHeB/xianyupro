import { chromium, defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: ".",
  testMatch: "frontend.spec.cjs",
  workers: 1,
  use: {
    baseURL: "http://127.0.0.1:5175",
    launchOptions: { executablePath: chromium.executablePath() },
  },
  webServer: {
    command: "npm run dev -- --host 127.0.0.1 --port 5175 --strictPort",
    url: "http://127.0.0.1:5175",
    reuseExistingServer: false,
  },
});
