// 状态文案与 tag 颜色映射（列表页列配置用）

// 状态类字段统一映射为 tag 颜色（先过 format 再查色）
export function tagType(value) {
  const map = {
    // 通用成功/失败
    active: "success", ok: "success", confirmed: "success", success: "success",
    delivered: "success", sold_out: "danger", expired: "danger", error: "danger",
    failed: "danger",
    // 发货状态机（docs/后端接口文档.md §6）
    pending: "warning", message_sending: "warning", message_confirmed: "primary",
    platform_confirmed: "primary", skipped: "info", unknown: "danger",
    // 账号 / Cookie
    normal: "success", invalid: "danger", 启用: "success", 停用: "danger",
    待校验: "warning", 正常: "success", 过期: "danger",
    // 商品
    下架: "info", 在售: "success", 已售: "danger",
    // AI 回复人设
    已设置: "success", 跟随全局: "info",
    // 订单（0待付款-5已关闭）
    待付款: "warning", 已付款: "warning", 待发货: "primary", 已发货: "success",
    已完成: "success", 已关闭: "danger",
    // 商品池
    approved: "success", rejected: "danger",
    // 采集池状态机（设计文档 §3.2）：SOURCED→DECIDED→MAPPED→PUSHED/FAILED
    SOURCED: "info", DECIDED: "warning", MAPPED: "primary", PUSHED: "success", FAILED: "danger",
    // 定价模式（§7.1 三级降级链）：manual 必须标红
    llm: "success", "fallback-rule": "warning", manual: "danger",
    // 消息方向
    in: "info", out: "primary",
    // 品类
    A: "primary", B: "warning",
    // 商品池状态机翻成人话后仍要保色（DataTable 的 tag 列先过 format 再查色）
    仅采集待定价: "info", 已定价待放行: "warning", 已放行待上架: "primary",
    已上架: "success", 上架失败: "danger",
  };
  return map[value] ?? "info";
}

// 采集/商品池状态机（§3.2）→ 使用者语言。页面上不再露 SOURCED/DECIDED/MAPPED。
export function poolStatusText(v) {
  return {
    SOURCED: "仅采集待定价",
    DECIDED: "已定价待放行",
    MAPPED: "已放行待上架",
    PUSHED: "已上架",
    FAILED: "上架失败",
  }[v] ?? String(v ?? "-");
}

// 定价来源（§7.1 三级降级链）
export function pricingText(v) {
  return { llm: "模型定价", "fallback-rule": "规则定价", manual: "需人工填价" }[v] ?? String(v ?? "");
}

// 文案来源。成品文案只应出自模型；none = 那一轮没产出来。
export function copySourceText(v) {
  return { llm: "模型撰写", human: "人工改写", none: "文案未产出" }[v] ?? String(v ?? "-");
}

export function cookieStatusText(v) {
  return { 0: "待校验", 1: "正常", 2: "过期" }[v] ?? String(v);
}

export function goodsStatusText(v) {
  return { 0: "下架", 1: "在售", 2: "已售" }[v] ?? String(v);
}

export function orderStatusText(v) {
  return { 0: "待付款", 1: "已付款", 2: "待发货", 3: "已发货", 4: "已完成", 5: "已关闭" }[v] ?? String(v);
}

export function formatCent(v) {
  return v == null ? "-" : (v / 100).toFixed(2) + " 元";
}

export function formatMsTime(ms) {
  return ms ? new Date(Number(ms)).toLocaleString() : "-";
}

// 从 fetch 抛出的 "400 {\"detail\":\"...\"}" 里提取后端中文原因
export function errText(e) {
  const raw = String(e?.message || e);
  const m = raw.match(/\{.*\}/s);
  if (m) {
    try {
      return JSON.parse(m[0]).detail ?? raw;
    } catch {
      /* 非 JSON，原样返回 */
    }
  }
  return raw;
}
