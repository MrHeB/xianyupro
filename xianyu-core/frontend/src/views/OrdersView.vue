<script setup>
// 订单列表（GET /api/orders）：账号跟随顶栏全局「当前账号」；金额/状态由 order_sync 调度任务补齐
import { onMounted, ref, watch } from "vue";
import { api } from "../api";
import DataTable from "../components/DataTable.vue";
import { useAccountColumns, useAccounts } from "../composables/useAccounts";
import { formatCent, orderStatusText, errText } from "../utils/format";

const loading = ref(false);
const data = ref([]);
const error = ref("");
const { currentAccountId, loadAccounts } = useAccounts();

const columns = useAccountColumns([
  { prop: "id", label: "ID", width: 70 },
  { prop: "order_id", label: "订单号", minWidth: 160, showOverflowTooltip: true },
  { prop: "category", label: "品类", width: 80, tag: true },
  { prop: "goods_id", label: "商品 ID", width: 90 },
  { prop: "buyer_name", label: "买家", width: 120 },
  { prop: "total_amount_cent", label: "金额", width: 100, format: formatCent },
  { prop: "order_status", label: "状态", tag: true, format: orderStatusText },
  { prop: "paid_at", label: "支付时间", width: 170 },
]);

async function reload() {
  loading.value = true;
  try {
    data.value = await api.listOrders(currentAccountId.value || undefined);
    error.value = "";
  } catch (e) {
    error.value = errText(e);
  } finally {
    loading.value = false;
  }
}

// 顶栏切号即刷新，页内不再放第二个账号控件
watch(currentAccountId, reload);
onMounted(() => {
  loadAccounts();
  reload();
});
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="header">
        <span>订单（WS 付款事件实时落库 + 订单同步任务补齐金额/状态）</span>
        <el-button text @click="reload">刷新</el-button>
      </div>
    </template>

    <el-alert v-if="error" type="error" :title="'加载失败：' + error" :closable="false" show-icon />
    <DataTable v-else :columns="columns" :data="data" :loading="loading" />
  </el-card>
</template>

<style scoped>
.header { display: flex; justify-content: space-between; align-items: center; }
</style>
