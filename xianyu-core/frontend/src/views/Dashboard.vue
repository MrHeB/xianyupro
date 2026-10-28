<script setup>
// 工作台（阶段 F2）：账号健康 / 今日消息 / 待上架 / 待发货 概览卡片，数据源 GET /api/stats/overview
// 数字跟着顶栏「当前账号」走，所以卡片上必须写清统计范围，否则单号的 3 件会被当成全站的 3 件。
import { computed, onMounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { api } from "../api";
import { accountName, useAccounts } from "../composables/useAccounts";
import { errText } from "../utils/format";

const router = useRouter();
const { currentAccountId } = useAccounts();
const loading = ref(true);
const error = ref("");
const stats = ref(null);
// 写操作止损熔断（风控手册 R-9）：必须在工作台可见，不能只躺在日志里
const guards = ref([]);

const DOMAIN_TEXT = { publish: "上架", reply: "回复", delivery: "发货" };
const scopeText = computed(() =>
  currentAccountId.value == null ? "全部账号" : accountName(currentAccountId.value),
);
const visibleGuards = computed(() =>
  guards.value.filter(
    (g) =>
      (g.paused || g.failures > 0) &&
      (currentAccountId.value == null || g.account_id === currentAccountId.value),
  ),
);

const cards = [
  {
    key: "accounts",
    title: "闲鱼账号健康",
    to: "/accounts",
    lines: (s) => [
      { label: scopeText.value, value: s.accounts.total },
      { label: "启用中", value: s.accounts.enabled },
      { label: "Cookie 正常", value: s.accounts.cookie_ok },
    ],
  },
  {
    key: "goods",
    title: "待上架 / 在售",
    to: "/goods",
    lines: (s) => [
      { label: "待上架", value: s.goods.pending_publish, hot: true },
      { label: "在售", value: s.goods.on_sale },
    ],
  },
  {
    key: "collect",
    title: "采集池",
    to: "/collect/workspace",
    lines: (s) => [
      // 待确认 = SOURCED/DECIDED（未放行的采集件）；已放行 = MAPPED（等 auto_listing 发出）
      { label: "待人工确认", value: s.goods.to_confirm, hot: true },
      { label: "已放行待上架", value: s.goods.approved },
    ],
  },
  {
    key: "orders",
    title: "订单",
    to: "/orders",
    lines: (s) => [
      { label: "待发货", value: s.orders.pending_delivery, hot: true },
      { label: "今日付款", value: s.orders.today },
    ],
  },
  {
    key: "messages",
    title: "今日消息",
    to: "/messages",
    lines: (s) => [{ label: "收到消息", value: s.messages.today_in }],
  },
  {
    key: "delivery",
    title: "发货",
    to: "/delivery",
    lines: (s) => [
      { label: "待处理", value: s.delivery.pending, hot: true },
      { label: "今日发货", value: s.delivery.today },
    ],
  },
];

async function load() {
  loading.value = true;
  try {
    stats.value = await api.getStats(currentAccountId.value || undefined);
    error.value = "";
  } catch (e) {
    error.value = errText(e);
  }
  try {
    guards.value = await api.getGuardStatus();
  } catch {
    /* 熔断状态读不到不影响工作台主数据 */
  } finally {
    loading.value = false;
  }
}

watch(currentAccountId, load);
onMounted(load);
</script>

<template>
  <div v-loading="loading">
    <el-alert v-if="error" type="error" :title="'加载失败：' + error" :closable="false" show-icon />
    <template v-else-if="stats">
      <el-alert
        v-for="g in visibleGuards"
        :key="g.key"
        :type="g.paused ? 'error' : 'warning'"
        :closable="false"
        show-icon
        class="guard-alert"
        :title="g.paused
          ? `账号 #${g.account_id} 的「${DOMAIN_TEXT[g.domain] || g.domain}」写操作已熔断（连续失败 ${g.failures} 次），剩余约 ${Math.ceil(g.remaining_seconds / 60)} 分钟`
          : `账号 #${g.account_id} 的「${DOMAIN_TEXT[g.domain] || g.domain}」已记 ${g.failures} 次写失败（满 3 次熔断 24 小时）`"
        description="禁止重试绕过；重新扫码登录该账号（Cookie 更新）即可自动解锁，或等暂停到期。"
      />
      <div class="grid">
        <el-card
          v-for="card in cards"
          :key="card.key"
          class="stat-card"
          shadow="hover"
          @click="router.push(card.to)"
        >
          <div class="stat-title">{{ card.title }}</div>
          <div class="stat-lines">
            <div v-for="item in card.lines(stats)" :key="item.label" class="stat-line">
              <span class="stat-label">{{ item.label }}</span>
              <span class="stat-value" :class="{ hot: item.hot && item.value > 0 }">
                {{ item.value }}
              </span>
            </div>
          </div>
        </el-card>
      </div>
      <p class="hint">
        统计范围：{{ scopeText }}（在顶栏「当前账号」切换）。点击卡片进入对应页面；"待上架 / Cookie 异常 / 待发货"为今日优先处理项。
      </p>
    </template>
  </div>
</template>

<style scoped>
.guard-alert { margin-bottom: 12px; }
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 16px;
}
.stat-card { cursor: pointer; }
.stat-title { font-size: 14px; color: var(--el-text-color-secondary); margin-bottom: 10px; }
.stat-line { display: flex; justify-content: space-between; align-items: center; padding: 4px 0; }
.stat-label { font-size: 13px; color: var(--el-text-color-regular); }
.stat-value { font-size: 20px; font-weight: 700; }
.stat-value.hot { color: var(--el-color-danger); }
.hint { margin-top: 12px; font-size: 12px; color: var(--el-text-color-secondary); }
@media (max-width: 768px) {
  .grid { grid-template-columns: 1fr 1fr; }
}
</style>