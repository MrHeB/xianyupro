<script setup>
// 消息页：日常看买家消息的地方，顶部直接控制 AI 自动回复（与 LLM 配置页共用同一设置）。
// 账号跟随顶栏全局「当前账号」，本页只留买家会话下钻；列表每 10s 自动刷新。
import { onBeforeUnmount, onMounted, ref, watch } from "vue";
import { api } from "../api";
import DataTable from "../components/DataTable.vue";
import { accountName, useAccountColumns, useAccounts } from "../composables/useAccounts";
import { formatMsTime, errText } from "../utils/format";

const loading = ref(false);
const data = ref([]);
const error = ref("");
const conversationId = ref(null);
const conversations = ref([]);
const { currentAccountId, loadAccounts } = useAccounts();

// —— AI 自动回复开关（GET/PUT /settings/reply-mode）——
const modeInfo = ref(null);
const modeBusy = ref(false);
const isAuto = ref(false);

async function loadMode() {
  try {
    modeInfo.value = await api.getReplyMode();
    isAuto.value = modeInfo.value.mode === "auto";
  } catch {
    modeInfo.value = null;
  }
}

async function toggleAuto(val) {
  modeBusy.value = true;
  try {
    const r = await api.setReplyMode(val ? "auto" : "manual");
    isAuto.value = r.mode === "auto";
    if (modeInfo.value) modeInfo.value.mode = r.mode;
  } catch (e) {
    isAuto.value = !val; // 回滚开关
    error.value = errText(e);
  } finally {
    modeBusy.value = false;
  }
}

function convLabel(c) {
  const who = c.peer_name || c.peer_id;
  const prefix = currentAccountId.value != null ? "" : `【${accountName(c.account_id)}】`;
  const preview = c.last_content ? `｜${c.last_content}` : "";
  return `${prefix}${who}${preview}`;
}

const columns = useAccountColumns([
  { prop: "message_time", label: "时间", width: 170, format: formatMsTime },
  {
    prop: "conversation_id",
    label: "买家",
    width: 140,
    format: (id) => {
      const c = conversations.value.find((x) => x.id === id);
      return c ? c.peer_name || c.peer_id : `会话#${id}`;
    },
    showOverflowTooltip: true,
  },
  { prop: "direction", label: "方向", width: 80, tag: true },
  { prop: "content", label: "内容", minWidth: 240, showOverflowTooltip: true },
  { prop: "is_auto_reply", label: "自动回复", bool: true, width: 90 },
]);

async function reloadConversations() {
  try {
    conversations.value = await api.listConversations(currentAccountId.value || undefined);
    if (conversationId.value && !conversations.value.some((c) => c.id === conversationId.value)) {
      conversationId.value = null;
    }
  } catch (e) {
    conversations.value = [];
    error.value = errText(e);
  }
}

async function reload() {
  loading.value = true;
  try {
    data.value = await api.listMessages(
      currentAccountId.value || undefined,
      conversationId.value || undefined
    );
    error.value = "";
  } catch (e) {
    error.value = errText(e);
  } finally {
    loading.value = false;
  }
}

async function refreshAll() {
  await reloadConversations();
  await reload();
}

watch(currentAccountId, refreshAll);
watch(conversationId, reload);

let timer = null;
onMounted(async () => {
  await loadAccounts();
  await Promise.all([refreshAll(), loadMode()]);
  timer = setInterval(refreshAll, 10000);
});
onBeforeUnmount(() => timer && clearInterval(timer));
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="header">
        <div class="reply-ctl">
          <span class="ctl-label">AI 自动回复</span>
          <el-switch
            v-model="isAuto"
            :loading="modeBusy"
            :disabled="!modeInfo || modeBusy"
            inline-prompt
            active-text="开"
            inactive-text="关"
            @change="toggleAuto"
          />
          <span v-if="modeInfo" class="ctl-hint">
            {{ isAuto ? "买家消息将自动回复" : "不自动回复，需人工处理" }}
            <template v-if="modeInfo.mode === 'hybrid'">（当前为混合模式，见 LLM 配置）</template>
          </span>
          <el-tooltip
            v-if="modeInfo && !modeInfo.llm_configured"
            content="未配置 API Key，自动模式不会真正生成回复，请去 LLM 配置页填写"
            placement="top"
          >
            <el-tag type="danger" size="small">LLM 未配置</el-tag>
          </el-tooltip>
          <router-link to="/settings/llm" class="cfg-link">回复设置</router-link>
        </div>
        <el-button text @click="refreshAll">刷新</el-button>
      </div>
    </template>

    <div class="toolbar">
      <span class="label">买家：</span>
      <el-select
        v-model="conversationId"
        filterable
        clearable
        placeholder="全部会话"
        style="width: 320px"
      >
        <el-option
          v-for="c in conversations"
          :key="c.id"
          :value="c.id"
          :label="convLabel(c)"
        >
          <span>{{ convLabel(c) }}</span>
          <span class="conv-time">{{ formatMsTime(c.last_message_time) }}</span>
        </el-option>
      </el-select>
    </div>

    <el-alert v-if="error" type="error" :title="'加载失败：' + error" :closable="false" show-icon />
    <DataTable v-else :columns="columns" :data="data" :loading="loading" />
  </el-card>
</template>

<style scoped>
.header { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
.reply-ctl { display: flex; align-items: center; gap: 10px; }
.ctl-label { font-weight: 600; }
.ctl-hint { color: var(--el-text-color-secondary); font-size: 0.85em; }
.cfg-link { color: var(--el-color-primary); font-size: 0.85em; text-decoration: none; }
.toolbar { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; }
.label { color: var(--el-text-color-secondary); font-size: 0.9em; }
.conv-time { float: right; color: var(--el-text-color-secondary); font-size: 0.85em; margin-left: 16px; }
</style>
