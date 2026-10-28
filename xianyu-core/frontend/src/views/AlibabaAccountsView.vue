<script setup>
// 1688 货源账号（GET /api/alibaba/accounts）：采集用的登录态，与闲鱼发布账号两套 Cookie。
// 没有它，详情页只能匿名抓 → 必被 x5sec punish（采集判 blocked，不入库）。
import { onMounted, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { api } from "../api";
import DataTable from "../components/DataTable.vue";
import QrScanner from "../components/QrScanner.vue";
import { cookieStatusText, errText } from "../utils/format";

const loading = ref(false);
const data = ref([]);
const error = ref("");
const bindVisible = ref(false);

const columns = [
  { prop: "id", label: "ID", width: 70 },
  { prop: "nickname", label: "昵称" },
  { prop: "external_uid", label: "unb" },
  { prop: "status", label: "状态", tag: true, format: (v) => (v === 1 ? "启用" : "停用") },
  { prop: "cookie_status", label: "Cookie 状态", tag: true, format: cookieStatusText },
  { prop: "last_login_at", label: "最近登录" },
  { prop: "remark", label: "备注" },
];

async function reload() {
  loading.value = true;
  try {
    data.value = await api.listAlibabaAccounts();
    error.value = "";
  } catch (e) {
    error.value = errText(e);
  } finally {
    loading.value = false;
  }
}

function onBound() {
  bindVisible.value = false;
  reload();
}

async function unbind(row) {
  const ok = await ElMessageBox.confirm(
    `解绑后 1688 采集会退回匿名抓取（大概率被风控拦截）。已采到的商品不受影响，确认解绑 #${row.id}？`,
    "解绑货源账号",
    { type: "warning", confirmButtonText: "解绑", cancelButtonText: "取消" },
  )
    .then(() => true)
    .catch(() => false);
  if (!ok) return;
  try {
    await api.deleteAlibabaAccount(row.id);
    ElMessage.success(`已解绑 #${row.id}`);
    reload();
  } catch (e) {
    ElMessage.error(errText(e));
  }
}

onMounted(reload);
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="header">
        <span>1688 货源账号（只用于采集详情页，不能上架；上架仍走闲鱼账号）</span>
        <div>
          <el-button type="primary" size="small" @click="bindVisible = true">扫码绑定</el-button>
          <el-button text @click="reload">刷新</el-button>
        </div>
      </div>
    </template>
    <el-alert v-if="error" type="error" :title="'加载失败：' + error" :closable="false" show-icon />
    <el-alert
      v-else-if="!loading && !data.length"
      type="warning"
      :closable="false"
      show-icon
      title="还没有 1688 货源账号"
    >
      <template #default>
        匿名抓 1688 详情页会被 x5sec 风控拦截，采集会全部判 blocked 且不入库。
        请先扫码绑定一个货源账号。
      </template>
    </el-alert>
    <DataTable v-else :columns="columns" :data="data" :loading="loading">
      <template #action="{ row }">
        <el-button text type="primary" size="small" @click="bindVisible = true">重新扫码</el-button>
        <el-button text type="danger" size="small" @click="unbind(row)">解绑</el-button>
      </template>
    </DataTable>
  </el-card>

  <el-dialog
    v-model="bindVisible"
    title="1688 扫码绑定货源账号"
    width="380px"
    :close-on-click-modal="false"
  >
    <el-alert
      type="info"
      :closable="false"
      show-icon
      title="用 1688/淘宝 App 扫码；Cookie 加密入库，同一 unb 重复扫码只更新登录态"
      class="tip"
    />
    <div class="scanner-box">
      <QrScanner platform="alibaba" :size="240" @confirmed="onBound" />
    </div>
    <template #footer>
      <el-button text @click="bindVisible = false">关 闭</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.header { display: flex; justify-content: space-between; align-items: center; }
.tip { margin-bottom: 12px; }
.scanner-box { display: flex; justify-content: center; padding: 4px 0 12px; }
</style>
