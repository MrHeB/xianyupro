// 后端 API 薄封装（与 AI Agent 共用同一套接口，见 docs/后端接口文档.md 与 /docs Swagger）
// 阶段 F1：请求自动带 Bearer token；401 清 token 并回系统登录页。
const BASE = "/api";
const TOKEN_KEY = "xy_token";
const ROLE_KEY = "xy_role";
const NAME_KEY = "xy_name";

export function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}
export function getUserMeta() {
  return {
    username: localStorage.getItem(NAME_KEY) || "",
    role: localStorage.getItem(ROLE_KEY) || "",
  };
}
export function setAuth(token, user) {
  localStorage.setItem(TOKEN_KEY, token);
  if (user) {
    localStorage.setItem(ROLE_KEY, user.role || "user");
    localStorage.setItem(NAME_KEY, user.username || "");
  }
}
export function clearAuth() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(ROLE_KEY);
  localStorage.removeItem(NAME_KEY);
}

async function request(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  const resp = await fetch(`${BASE}${path}`, { ...options, headers });
  if (resp.status === 401) {
    // 未登录 / token 过期：清登录态回系统登录页
    clearAuth();
    if (!window.location.pathname.startsWith("/#")) {
      window.location.href = "/";
    }
    throw new Error("登录已过期，请重新登录");
  }
  if (!resp.ok) {
    const body = await resp.text();
    // errText() 靠 `{"detail":"…"}` 抠中文原因，JSON 体原样留着；
    // 网关（nginx 504/502）回的是整页 HTML，直接拼进 message 会把面板铺满。
    const brief = body.trimStart().startsWith("{")
      ? body
      : body.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim().slice(0, 80);
    throw new Error(`${resp.status} ${brief || resp.statusText}`);
  }
  return resp.json();
}

const get = (path, params) =>
  request(path + (params ? `?${new URLSearchParams(params).toString()}` : ""));
const post = (path, body) => request(path, { method: "POST", body: JSON.stringify(body ?? {}) });
const put = (path, body) => request(path, { method: "PUT", body: JSON.stringify(body ?? {}) });
const patch = (path, body) => request(path, { method: "PATCH", body: JSON.stringify(body ?? {}) });
const del = (path) => request(path, { method: "DELETE" });

export const api = {
  // ---- 系统用户（阶段 F1） ----
  /** 登录成功即把 token/用户信息写入 localStorage */
  login: async (username, password) => {
    const data = await post("/auth/login", { username, password });
    setAuth(data.access_token, data.user);
    return data;
  },
  register: (body) => post("/auth/register", body),
  me: () => get("/auth/me"),
  logout: () => post("/auth/logout"),

  // 账号（阶段 4-1 auth）
  listAccounts: () => get("/accounts"),
  startQrLogin: () => post("/accounts/qr-login"),
  pollQrLogin: (sessionId) => get(`/accounts/qr-login/${sessionId}`),
  /** 启用/停用（下线）：停用会当场停该账号 WS 并移出会话池 */
  setAccountStatus: (id, status) => patch(`/accounts/${id}/status`, { status }),
  /** 解绑（软删除）：已采商品与历史不受影响 */
  deleteAccount: (id) => del(`/accounts/${id}`),
  /** 该账号的 AI 回复人设（全局打底 + 人设叠加），含最终生效提示词预览 */
  getReplyConfig: (id) => get(`/accounts/${id}/reply-config`),
  /** 写人设；空字符串 = 取消本账号覆盖，回退全局提示词 */
  saveReplyConfig: (id, personaPrompt) => put(`/accounts/${id}/reply-config`, { persona_prompt: personaPrompt }),

  // 1688 货源账号（采集用的登录态，与闲鱼发布账号分开两套 Cookie）
  listAlibabaAccounts: () => get("/alibaba/accounts"),
  startAlibabaQrLogin: () => post("/alibaba/qr-login"),
  pollAlibabaQrLogin: (sessionId) => get(`/alibaba/qr-login/${sessionId}`),
  deleteAlibabaAccount: (id) => del(`/alibaba/accounts/${id}`),

  // 商品
  listGoods: (category, accountId) =>
    get("/goods", { ...(category ? { category } : {}), ...(accountId ? { account_id: accountId } : {}) }),
  createGoods: (body) => post("/goods", body),
  importCsv: (body) => post("/goods/import-csv", body),
  // 1688 采集统一走下方 /collect/run（offer_ids / keyword / instruction）；老的 /goods/collect-1688 前端不再调
  publishGoods: (goodsId) => post(`/goods/${goodsId}/publish`),
  /** 人工放行（DECIDED → MAPPED）：单次 ≤5 件，任一件不合格整批拒绝 */
  batchApprove: (goodsIds) => post("/goods/batch-approve", { goods_ids: goodsIds, confirm: true }),

  // ==== 采集工作区（data/collect 可视化） ====
  listCollectBatches: (limit = 50) => get("/collect/batches", { limit }),
  getCollectBatch: (batchId) => get(`/collect/batches/${encodeURIComponent(batchId)}`),
  getCollectOffer: (batchId, offerId) =>
    get(`/collect/offers/${encodeURIComponent(batchId)}/${encodeURIComponent(offerId)}`),
  /** 跑 LLM 定价 + 文案 + 风险扫描（§8.4 节点 2–4），只写本地，不触达闲鱼 */
  decideOffer: (batchId, offerId, body = {}) =>
    post(`/collect/offers/${encodeURIComponent(batchId)}/${encodeURIComponent(offerId)}/decide`, body),
  listPending: (poolStatus = "DECIDED", accountId) =>
    get("/collect/pending", { pool_status: poolStatus, ...(accountId ? { account_id: accountId } : {}) }),

  // ==== 自然语言采集编排（需求文档 §3 五节点 / §4.4） ====
  /** 关键词 → offer_id 候选列表（一次性浏览器，不入库）。失败是 502 + 中文原因 */
  search1688: (body) => post("/collect/search-1688", body),
  /** 一句话（或 keyword/offer_ids）→ 采集 → 定价文案扫描 → 待确认清单。业务结论在 status，不是 HTTP 码 */
  runCollect: (body) => post("/collect/run", body),
  getCollectSchedule: () => get("/collect/schedule"),
  /** 仅管理员：`scheduled_task` 是全局行，没有归属列 */
  putCollectSchedule: (body) => put("/collect/schedule", body),

  /** 本地图片原件 URL（<img :src> 用） */
  collectImageUrl: (batchId, offerId, name) =>
    `${BASE}/collect/offers/${encodeURIComponent(batchId)}/${encodeURIComponent(offerId)}/images/${encodeURIComponent(name)}`,

  // 写操作止损熔断状态（风控手册 R-9）
  getGuardStatus: () => get("/accounts/guard"),

  // 订单
  listOrders: (accountId, limit = 50) =>
    get("/orders", { limit, ...(accountId ? { account_id: accountId } : {}) }),

  // 消息（可按账号/会话过滤）
  listMessages: (accountId, conversationId, limit = 50) =>
    get("/messages", {
      limit,
      ...(accountId ? { account_id: accountId } : {}),
      ...(conversationId ? { conversation_id: conversationId } : {}),
    }),
  listConversations: (accountId) => get("/messages/conversations", accountId ? { account_id: accountId } : {}),

  // 发货（品类 A 卡密）
  listDeliveryRecords: (accountId, limit = 50) =>
    get("/delivery/records", { limit, ...(accountId ? { account_id: accountId } : {}) }),
  listCardGroups: (accountId) => get("/delivery/cards", accountId ? { account_id: accountId } : {}),
  createCardGroup: (body) => post("/delivery/card-groups", body),
  importCardItems: (groupId, lines) => post(`/delivery/card-groups/${groupId}/items`, { lines }),
  upsertDeliveryRule: (body) => post("/delivery/rules", body),

  // 工作台概览（阶段 F2）：账号健康/待上架/待发货/今日消息
  getStats: (accountId) => get("/stats/overview", accountId ? { account_id: accountId } : undefined),

  // 设置：AI 回复模式（实时消息页用）
  getReplyMode: () => get("/settings/reply-mode"),
  setReplyMode: (mode) => put("/settings/reply-mode", { mode }),

  // ==== LLM 配置（阶段 F3，DB 覆盖 .env 免重启生效） ====
  getLlmConfig: () => get("/settings/llm"),
  updateLlmConfig: (cfg) => put("/settings/llm", cfg),
  testLlm: () => post("/settings/llm/test"),
};