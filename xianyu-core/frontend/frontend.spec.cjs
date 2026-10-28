const { test, expect } = require("@playwright/test");

const ACCOUNTS = [
  { id: 1, nickname: "小号A", external_uid: "a1", cookie_status: 1 },
  { id: 2, nickname: "小号B", external_uid: "b2", cookie_status: 0 },
];

async function mockRoute(route) {
  const url = new URL(route.request().url());
  const path = url.pathname;
  const json = (data, status = 200) =>
    route.fulfill({ status, contentType: "application/json", body: JSON.stringify(data) });

  if (path === "/api/accounts") return json(ACCOUNTS);
  if (path === "/api/goods") return json([
    { id: 11, title: "Steam 充值码", category: "A", source: "manual", price_cent: 1000, status: 1, pool_status: null, external_goods_id: "xy-11" },
    { id: 12, title: "采集长裙", category: "B", source: "csv", price_cent: 3900, status: 0, pool_status: "MAPPED", external_goods_id: null },
  ]);
  if (path === "/api/orders") return json([
    { id: 21, order_id: "o-100", category: "A", goods_id: 11, buyer_name: "买家一号", total_amount_cent: 1000, order_status: 3, paid_at: 1758000000000 },
  ]);
  if (path === "/api/messages") return json([
    { id: 31, message_time: 1758000000000, conversation_id: 555, direction: "in", content_type: "text", content: "在吗", is_auto_reply: 0 },
  ]);
  if (path === "/api/delivery/records") return json([
    { id: 41, order_id: "o-100", delivery_mode: "kami", status: "delivered", fail_reason: "" },
  ]);
  if (path === "/api/delivery/cards") return json([
    { id: 7, name: "Steam 组", goods_id: 11, total_count: 10, used_count: 8, available: 2, low_stock: true },
  ]);
  if (path === "/api/delivery/card-groups" && route.request().method() === "POST") {
    return json({ id: 8, name: "新建组", goods_id: 11, alert_threshold: 5 });
  }
  if (path.startsWith("/api/delivery/card-groups/")) return json({ added: 3, total_count: 13 });
  if (path === "/api/delivery/rules" && route.request().method() === "POST") {
    const body = route.request().postDataJSON();
    if (!body.account_id) return json({ detail: "账号必填" }, 400);
    return json(body);
  }
  if (path === "/api/settings/reply-mode") return json({ mode: "manual", llm_configured: false, model: "gpt-4o-mini", max_daily_replies: 200 });
  if (path === "/api/accounts/qr-login" && route.request().method() === "POST") {
    return json({ session_id: "s-1", qr_image: "data:image/png;base64,iVBORw0KGgo=" });
  }
  if (path === "/api/accounts/qr-login/s-1") {
    return json({ status: "scaned", nickname: "小号A" });
  }
  return json({ detail: "未模拟: " + path }, 404);
}

test.beforeEach(async ({ page }) => {
  await page.route("**/api/**", mockRoute);
});

test("列表页与弹窗冒烟", async ({ page }) => {
  await page.goto("/orders");
  await expect(page.getByRole("link", { name: "商品" })).toBeVisible();
  await expect(page.locator(".el-table__row")).toContainText("o-100");
  await page.getByRole("button", { name: "全部账号" }).click();
  await page.locator(".el-select-dropdown__item", { hasText: "小号A" }).first().click();
  await page.getByRole("button", { name: "查询", exact: true }).click();

  await page.goto("/messages");
  await expect(page.locator(".el-table__row")).toContainText("在吗");
  await page.getByRole("button", { name: "查询", exact: true }).click();

  await page.goto("/delivery");
  await expect(page.locator(".el-table__row")).toContainText("delivered");
  await page.getByRole("button", { name: "配置发货规则" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.getByRole("button", { name: "保存" }).click();
  await page.waitForTimeout(150);
  await expect(page.getByRole("dialog")).toBeHidden();

  await page.goto("/accounts");
  await expect(page.locator(".el-table__row", { hasText: "小号B" })).toContainText("待校验");

  await page.goto("/cards");
  await expect(page.locator(".el-table__row")).toContainText("Steam 组");
  await page.getByRole("button", { name: "新建卡密组" }).click();
  await page.getByRole("textbox").first().fill("新建组");
  await page.getByRole("dialog").getByRole("button", { name: "保存" }).click();
  await expect(page.getByRole("dialog")).toBeHidden();
});

test("扫码登录轮询并提示", async ({ page }) => {
  await page.goto("/login");
  await page.getByRole("button", { name: "生成登录二维码" }).click();
  await expect(page.locator(".el-image")).toBeVisible();
  await expect(page.getByText("已扫码，请在手机上确认登录")).toBeVisible();
  await expect(page.locator(".el-alert").first()).toBeVisible();
  await page.waitForTimeout(1100);
});

test("实时消息：离开后不再重连", async ({ page }) => {
  let wsCount = 0;
  page.on("websocket", (ws) => { wsCount += 1; });
  await page.addInitScript(() => {
    window.WebSocket = class {
      constructor(url) {
        this.url = url;
        setTimeout(() => this.onclose && this.onclose({}), 30);
      }
      close() {}
      send() {}
    };
  });
  await page.goto("/live");
  await page.waitForTimeout(400);
  await page.getByRole("link", { name: "商品" }).click();
  await page.waitForTimeout(7000);
  expect(wsCount).toBeLessThanOrEqual(2);
});
