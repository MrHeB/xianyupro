<script setup>
// 账号列表（GET /api/accounts）：昵称/状态，不含登录态
// F2：行内「重新扫码」入口，复用 QrScanner 组件（confirmed 后自动刷新列表）
// 下线/解绑：写操作，会当场停该账号 WS 并移出会话池（后端 accounts.py 负责）
// 回复人设：该账号 AI 回复风格（全局打底 + 叠加），保存后下一条消息即生效
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
const bindPending = ref(null);

// 回复人设（全局打底 + 本账号叠加）
const personaVisible = ref(false);
const personaRow = ref(null);
const personaText = ref("");
const personaGlobal = ref("");
const personaSaving = ref(false);

const columns = [
  { prop: "id", label: "ID", width: 70 },
  { prop: "nickname", label: "昵称" },
  { prop: "external_uid", label: "外部 UID" },
  { prop: "status", label: "状态", tag: true, format: (v) => (v === 1 ? "启用" : "停用") },
  { prop: "cookie_status", label: "Cookie 状态", tag: true, format: cookieStatusText },
  {
    prop: "has_persona",
    label: "回复人设",
    tag: true,
    width: 100,
    format: (v) => (v ? "已设置" : "跟随全局"),
  },
  { prop: "fish_shop_user", label: "鱼小铺", bool: true, width: 90 },
  { prop: "last_login_at", label: "最近登录" },
];

async function reload() {
  loading.value = true;
  try {
    data.value = await api.listAccounts();
    error.value = "";
  } catch (e) {
    error.value = errText(e);
  } finally {
    loading.value = false;
  }
}

function openBind(row = null) {
  bindPending.value = row;
  bindVisible.value = true;
}

function onBound() {
  if (!bindPending.value) bindVisible.value = false;
  reload();
}

async function toggleStatus(row) {
  const next = row.status === 1 ? 0 : 1;
  if (next === 0) {
    const ok = await ElMessageBox.confirm(
      `下线「${row.nickname || row.external_uid}」：立即停止该账号收消息、自动回复、上架与发货，重新启用后恢复。确认下线？`,
      "下线账号",
      { type: "warning", confirmButtonText: "下线", cancelButtonText: "取消" },
    )
      .then(() => true)
      .catch(() => false);
    if (!ok) return;
  }
  try {
    const r = await api.setAccountStatus(row.id, next);
    ElMessage.success(next === 0 ? "已下线" : r.note || "已启用");
    reload();
  } catch (e) {
    ElMessage.error(errText(e));
  }
}

async function unbind(row) {
  const ok = await ElMessageBox.confirm(
    `解绑「${row.nickname || row.external_uid}」：停止该账号所有自动化并移出列表（已采商品与历史不受影响）。确认解绑 #${row.id}？`,
    "解绑闲鱼账号",
    { type: "warning", confirmButtonText: "解绑", cancelButtonText: "取消" },
  )
    .then(() => true)
    .catch(() => false);
  if (!ok) return;
  try {
    await api.deleteAccount(row.id);
    ElMessage.success(`已解绑 #${row.id}`);
    reload();
  } catch (e) {
    ElMessage.error(errText(e));
  }
}

async function openPersona(row) {
  personaRow.value = row;
  try {
    const cfg = await api.getReplyConfig(row.id);
    personaText.value = cfg.persona_prompt ?? "";
    personaGlobal.value = cfg.global_system_prompt ?? "";
    personaVisible.value = true;
  } catch (e) {
    ElMessage.error(errText(e));
  }
}

async function savePersona() {
  personaSaving.value = true;
  try {
    await api.saveReplyConfig(personaRow.value.id, personaText.value);
    ElMessage.success(personaText.value.trim() ? "人设已保存，下一条消息生效" : "已清空，该账号回退全局提示词");
    personaVisible.value = false;
    reload();
  } catch (e) {
    ElMessage.error(errText(e));
  } finally {
    personaSaving.value = false;
  }
}

onMounted(reload);
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="header">
        <span>账号（一个登录用户可绑多个闲鱼号，Cookie 由后端会话池管理）</span>
        <div>
          <el-button type="primary" size="small" @click="openBind(null)">新增账号（扫码）</el-button>
          <el-button text @click="reload">刷新</el-button>
        </div>
      </div>
    </template>
    <el-alert v-if="error" type="error" :title="'加载失败：' + error" :closable="false" show-icon />
    <DataTable v-else :columns="columns" :data="data" :loading="loading" :action-width="280">
      <template #action="{ row }">
        <el-button text :type="row.status === 1 ? 'warning' : 'success'" size="small" @click="toggleStatus(row)">
          {{ row.status === 1 ? "下线" : "启用" }}
        </el-button>
        <el-button text type="primary" size="small" @click="openBind(row)">重新扫码</el-button>
        <el-button text type="primary" size="small" @click="openPersona(row)">回复人设</el-button>
        <el-button text type="danger" size="small" @click="unbind(row)">解绑</el-button>
      </template>
    </DataTable>
  </el-card>

  <el-dialog
    v-model="bindVisible"
    title="闲鱼扫码绑号"
    width="380px"
    :close-on-click-modal="false"
    @open="bindPending = null"
  >
    <el-alert
      v-if="bindPending"
      type="info"
      :closable="false"
      show-icon
      title="已存在该闲鱼号，扫码确认后将更新 Cookie 并归属到当前用户"
      class="tip"
    />
    <div class="scanner-box">
      <QrScanner
        :size="240"
        @confirmed="onBound"
      />
    </div>
    <template #footer>
      <el-button text @click="bindVisible = false">关 闭</el-button>
    </template>
  </el-dialog>

  <el-dialog
    v-model="personaVisible"
    :title="`回复人设 · ${personaRow?.nickname || personaRow?.external_uid || ''}`"
    width="620px"
  >
    <el-alert
      type="info"
      :closable="false"
      show-icon
      title="这里写的内容会追加在全局提示词后面，只决定这个号「说话像谁」；禁词、回复时段等合规设置仍走全局配置。留空 = 完全跟随全局。保存后下一条买家消息即生效。"
      class="tip"
    />
    <el-input
      v-model="personaText"
      type="textarea"
      :rows="7"
      maxlength="4000"
      show-word-limit
      placeholder="例：你是个改了机的数码发烧友，说话直接、爱用短句，习惯称呼对方「兄弟」；只谈成色和配件，不承诺保修；被问到底价就说「诚心要可以再聊一点」。"
    />
    <el-collapse class="advanced">
      <el-collapse-item title="查看全局打底提示词" name="global">
        <pre class="prompt">{{ personaGlobal || "（全局提示词为空，请到「系统设置」填写）" }}</pre>
      </el-collapse-item>
    </el-collapse>
    <template #footer>
      <el-button text @click="personaVisible = false">取 消</el-button>
      <el-button type="primary" :loading="personaSaving" @click="savePersona">保 存</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.header { display: flex; justify-content: space-between; align-items: center; }
.tip { margin-bottom: 12px; }
.scanner-box { display: flex; justify-content: center; padding: 4px 0 12px; }
.advanced { margin-top: 12px; }
.prompt { margin: 0; white-space: pre-wrap; word-break: break-all; font-size: 12px; line-height: 1.6; color: var(--el-text-color-secondary); }
</style>