const { chromium } = require("@playwright/test");
(async () => {
  const browser = await chromium.launch({ executablePath: chromium.executablePath() });
  const page = await browser.newPage();
  page.on("console", (m) => console.log("[console:" + m.type() + "]", m.text()));
  page.on("pageerror", (e) => console.log("[pageerror]", e.message));
  page.on("requestfailed", (r) => console.log("[reqfail]", r.url(), r.failure()?.errorText));
  await page.route("**/api/**", (route) => route.fulfill({ status: 200, contentType: "application/json", body: "[]" }));
  await page.goto("http://127.0.0.1:5175/login");
  await page.waitForTimeout(3000);
  console.log("MAIN_HTML_LEN:", (await page.locator(".el-main").innerHTML()).length);
  await browser.close();
})();
