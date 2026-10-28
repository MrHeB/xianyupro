<script setup>
// 1688 采集页（需求文档 N1）：一句话 → 后端 pipeline 跑到「待确认清单」就停。
// 三条入口（一句话 / 检索后勾选 / 粘贴 offer_id）打的是同一个 `POST /api/collect/run`，
// 只差在带哪个字段——分开写就会长成"这条能跑那条不能跑"的三套逻辑。
// 放行闸门刻意不在这页：`canApprove` 那套判据只住在采集工作区，这里只给结论与入口。
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import { api, getUserMeta } from "../api";
import { useAccounts } from "../composables/useAccounts";
import { useAlibabaAccounts } from "../composables/useAlibabaAccounts";
import { errText, formatCent, pricingText, tagType } from "../utils/format";

const { accounts, currentAccountId, loadAccounts, setCurrentAccountId } = useAccounts();
const { alibabaAccounts, loadAlibabaAccounts } = useAlibabaAccounts();
const router = useRouter();
const isAdmin = ref(getUserMeta().role === "admin");

const noSource = computed(() => !alibabaAccounts.value.length);
const usableSources = computed(() =>
  alibabaAccounts.value.filter((a) => a.status === 1 && a.cookie_status === 1),
);

// ---------------- 公共表单（三条入口共用账号与货源账号） ----------------

// 闲鱼账号是采集的落点，不是筛选条件，所以这一页仍留着行内选择器；
// 但它和顶栏「当前账号」双向同步，避免同屏两个控件各选各的。
const form = reactive({
  account_id: currentAccountId.value ?? null,
  alibaba_account_id: null,
  instruction: "",
  preference: "",
});

watch(currentAccountId, (id) => {
  if (id != null) form.account_id = id;
});

function onTargetAccountChange(v) {
  setCurrentAccountId(v);
}

function runBody(extra) {
  if (!form.account_id) {
    ElMessage.warning("请选择要发布到哪个闲鱼账号");
    return null;
  }
  return {
    account_id: Number(form.account_id),
    alibaba_account_id: form.alibaba_account_id ? Number(form.alibaba_account_id) : null,
    ...extra,
  };
}

// ---------------- ① 一句话采集 ----------------

const running = ref(false);
const runElapsedNote = ref("");
const runResult = ref(null);
const runError = ref("");

async function runCollect(body, tip) {
  if (!body) return;
  running.value = true;
  runResult.value = null;
  runError.value = "";
  runElapsedNote.value = tip;
  try {
    runResult.value = await api.runCollect(body);
  } catch (e) {
    // 同步长请求：网关到点断流时后台那一轮通常还在跑完，别让人以为白跑了
    runError.value = `${errText(e)}｜若是超时：批次多半已在后台跑完，去「采集工作区」刷新待确认清单再看一眼`;
  } finally {
    running.value = false;
    runElapsedNote.value = "";
  }
}

function submitInstruction() {
  const text = form.instruction.trim();
  if (!text) {
    ElMessage.warning("先说一句话，例如「找 3 款 Type-C 数据线，成本 5 元内，卖 19.9 左右」");
    return;
  }
  runCollect(
    runBody({
      instruction: text.slice(0, 500),
      preference: form.preference.trim().slice(0, 200) || null,
    }),
    "正在检索并逐件定价（开一次浏览器 + 每件 2 次模型调用，配了模型后几分钟也正常，别重复点）",
  );
}

// ---------------- ② 检索 → 勾选 → 采集 ----------------

const searchForm = reactive({ keyword: "", limit: 20 });
const searching = ref(false);
const searchResult = ref(null);
const searchError = ref("");
const picked = ref([]);
const pastedIds = ref("");

function parseIds(text) {
  return text
    .split(/[\n,，、\s]+/)
    .map((s) => s.trim())
    .filter((s) => s && !s.startsWith("#"));
}

async function doSearch() {
  const kw = searchForm.keyword.trim();
  if (!kw) {
    ElMessage.warning("输入检索关键词（1688 类目/品名，最多 60 字）");
    return;
  }
  searching.value = true;
  searchResult.value = null;
  searchError.value = "";
  picked.value = [];
  try {
    searchResult.value = await api.search1688({
      keyword: kw.slice(0, 60),
      limit: searchForm.limit ? Number(searchForm.limit) : null,
      alibaba_account_id: form.alibaba_account_id ? Number(form.alibaba_account_id) : null,
      max_pages: 2,
    });
  } catch (e) {
    searchError.value = errText(e);
  } finally {
    searching.value = false;
  }
}

const candidateRows = computed(() =>
  (searchResult.value?.offer_ids || []).map((id) => ({ offer_id: id })),
);

function submitCandidates() {
  const sel = picked.value.map((r) => r.offer_id);
  const ids = sel.length ? sel : parseIds(pastedIds.value);
  if (!ids.length) {
    ElMessage.warning("勾选至少一个候选，或直接粘贴 offer_id");
    return;
  }
  if (ids.length > 50) {
    ElMessage.warning("单次最多 50 件（后端同样限制），请分批");
    return;
  }
  runCollect(
    runBody({ offer_ids: ids }),
    `正在采集勾选的 ${ids.length} 件并逐件定价（每件之间有人性化延迟）`,
  );
}

// ---------------- ③ 定时采集计划 ----------------

const schedule = ref(null);
const scheduleError = ref("");
const saving = ref(false);

async function loadSchedule() {
  if (!isAdmin.value) return;
  try {
    const rows = await api.getCollectSchedule();
    schedule.value = rows[0] || null;
    scheduleError.value = "";
  } catch (e) {
    scheduleError.value = errText(e);
  }
}

async function saveSchedule(enabled) {
  const text = form.instruction.trim();
  if (!text) {
    ElMessage.warning("定时计划用的就是上面这句话，先把它写上");
    return;
  }
  const body = runBody({ instruction: text.slice(0, 500) });
  if (!body) return;
  if (enabled) {
    try {
      await ElMessageBox.confirm(
        "开启后最多 60 秒就会跑第一轮（不等满 30 分钟）：到点会自己开一次浏览器检索并逐件调用模型，" +
          "产出待确认清单为止——不会自动放行、不会自动上架。确认现在就开始定时？",
        "开启定时采集",
        { confirmButtonText: "开启", cancelButtonText: "取消", type: "warning" },
      );
    } catch {
      return;
    }
  }
  saving.value = true;
  try {
    schedule.value = await api.putCollectSchedule({ enabled, config: body });
    ElMessage.success(enabled ? "定时采集已开启" : "定时采集已关闭");
  } catch (e) {
    ElMessage.error(errText(e));
  } finally {
    saving.value = false;
  }
}

const scheduleText = computed(() => {
  const s = schedule.value;
  if (!s) return "还没有定时采集任务行";
  const cfg = s.config || {};
  return [
    s.enabled ? "运行中" : "已关闭",
    `每 ${Math.round((s.interval_seconds || 0) / 60)} 分钟`,
    `上次跑 ${s.last_run_at ? new Date(s.last_run_at).toLocaleString() : "从未"}`,
    `累计 ${s.run_attempt_count ?? 0} 次`,
    `连续未产出 ${s.consecutive_failure_count ?? 0} 次`,
  ].join(" · ");
});

// ---------------- 结果面板 ----------------

const decidedRows = computed(() => runResult.value?.decided || []);
const reviewableCount = computed(() => (runResult.value?.reviewable || []).length);
const STATUS_LABEL = {
  ok: "采集完成",
  needs_clarification: "需要你补充",
  no_source_account: "缺货源账号",
};
const statusLabel = computed(() => STATUS_LABEL[runResult.value?.status] || runResult.value?.status || "");
const planView = computed(() => {
  const p = runResult.value?.plan;
  if (!p) return "";
  const bits = [];
  if (p.keywords?.length) bits.push(`关键词 ${p.keywords.join("、")}`);
  if (p.offer_ids?.length) bits.push(`指定 ${p.offer_ids.length} 个 offer_id`);
  bits.push(`想要 ${p.quantity} 件`);
  if (p.cost_max_cent) bits.push(`成本上限 ${formatCent(p.cost_max_cent)}`);
  if (p.target_price_cent) bits.push(`目标售价 ${formatCent(p.target_price_cent)}`);
  if (p.preference) bits.push(`偏好 ${p.preference}`);
  return bits.join(" ｜ ");
});

const SEARCH_BLOCK_HINT = {
  risk_shell: "被 1688 风控拦下：先降频，去货源账号页确认 Cookie 还在",
  login_wall: "货源账号登录态已失效：重新扫码后再跑",
  no_result: "页面打开了但没解析出商品 ID：关键词太偏或列表页改版",
  empty_keyword: "关键词为空：这句里没读出可检索的品类，需要人工补一个词",
  quota_exceeded: "本小时该货源账号的检索次数已用完：下轮自动继续，不用手动重试",
};

function roundText(r) {
  if (r.blocked) return `「${r.keyword}」→ 检索被拦（${SEARCH_BLOCK_HINT[r.blocked] || r.blocked}）`;
  if (r.stop_reason) return `换词停下：${r.stop_reason}`;
  return `「${r.keyword}」→ 命中 ${r.found} 个`;
}

function reset() {
  form.instruction = "";
  form.preference = "";
  pastedIds.value = "";
  picked.value = [];
  searchResult.value = null;
  searchError.value = "";
  runResult.value = null;
  runError.value = "";
}

onMounted(() => {
  loadAccounts();
  loadAlibabaAccounts(true);
  loadSchedule();
});
</script>

<template>
  <div class="page">
    <el-card shadow="never">
      <template #header>
        <div class="header">
          <span>1688 采集（说人话 → 检索 → 定价文案 → 待确认清单）</span>
          <div>
            <el-button text @click="router.push('/collect/workspace')">采集工作区</el-button>
            <el-button text @click="router.push('/alibaba/accounts')">货源账号</el-button>
            <el-button text @click="loadAlibabaAccounts(true)">刷新</el-button>
          </div>
        </div>
      </template>

      <el-alert
        v-if="noSource"
        type="warning"
        :closable="false"
        show-icon
        title="尚未绑定可用的 1688 货源账号"
        class="warn"
      >
        <template #default>
          匿名检索与匿名详情页都只会被 1688 的 x5sec 风控拦下，这条链路在开浏览器之前就会拒绝启动。
          <el-link type="primary" @click="router.push('/alibaba/accounts')">去扫码绑定</el-link>
          （Cookie 数小时即失效，隔几天要重扫一次）
        </template>
      </el-alert>

      <el-form label-width="110px" :model="form" size="small">
        <el-form-item label="闲鱼账号" required>
          <el-select v-model="form.account_id" placeholder="选择要发布到哪个闲鱼号" style="width: 100%" @change="onTargetAccountChange">
            <el-option
              v-for="a in accounts"
              :key="a.id"
              :value="a.id"
              :label="`#${a.id} ${a.nickname || a.external_uid}`"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="1688 货源账号">
          <el-select
            v-model="form.alibaba_account_id"
            :placeholder="usableSources.length ? '留空 = 自动挑一个登录态可用的账号' : '无登录态可用的货源账号'"
            clearable
            style="width: 100%"
          >
            <el-option
              v-for="a in usableSources"
              :key="a.id"
              :value="a.id"
              :label="`#${a.id} ${a.nickname || a.external_uid}`"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="一句话需求" required>
          <el-input
            v-model="form.instruction"
            type="textarea"
            :rows="3"
            maxlength="500"
            show-word-limit
            placeholder="例：找 3 款 Type-C 快充数据线，成本 5 元以内，卖 19.9 左右；或：把 623375582529 这款采上来"
          />
          <p class="label">
            用大白话说清想要什么、大概几件、成本和卖价即可；信息不够时系统会反问，不会瞎猜。
          </p>
        </el-form-item>

        <el-form-item label="补充偏好">
          <el-input
            v-model="form.preference"
            maxlength="200"
            show-word-limit
            placeholder="选填：定价与文案要偏重的点，如「主打学生党，别写批发」"
          />
        </el-form-item>

        <el-form-item>
          <el-button type="primary" :loading="running" @click="submitInstruction">
            开始采集
          </el-button>
          <el-button :disabled="running" @click="reset">清空</el-button>
          <span v-if="running" class="label running">{{ runElapsedNote }}</span>
        </el-form-item>
      </el-form>

      <el-collapse class="advanced">
        <el-collapse-item title="进阶：先检索挑款、看清候选再采，或直接粘贴商品 ID" name="adv">
      <el-form :model="searchForm" size="small" inline>
        <el-form-item label="关键词">
          <el-input
            v-model="searchForm.keyword"
            style="width: 220px"
            maxlength="60"
            placeholder="1688 检索词，如 type-c 数据线"
          />
        </el-form-item>
        <el-form-item label="候选数">
          <el-input-number v-model="searchForm.limit" :min="1" :max="50" placeholder="默认 20" style="width: 120px" />
        </el-form-item>
        <el-form-item>
          <el-button :loading="searching" @click="doSearch">只检索，不入库</el-button>
        </el-form-item>
      </el-form>

      <el-alert v-if="searchError" type="error" :title="searchError" :closable="false" show-icon />
      <template v-if="searchResult">
        <p class="label">
          「{{ searchResult.keyword }}」检索到 {{ searchResult.count }} 个候选（翻了 {{ searchResult.pages }} 页）。
          检索只给商品 ID，标题与价格要采集之后才有。
        </p>
        <el-table
          :data="candidateRows"
          border
          stripe
          size="small"
          row-key="offer_id"
          max-height="260"
          @selection-change="(rows) => (picked = rows)"
        >
          <el-table-column type="selection" width="42" :reserve-selection="true" />
          <el-table-column prop="offer_id" label="商品 ID" width="160" />
          <el-table-column label="详情页">
            <template #default="{ row }">
              <a :href="`https://detail.1688.com/offer/${row.offer_id}.html`" target="_blank" rel="noreferrer noopener">
                打开看看
              </a>
            </template>
          </el-table-column>
        </el-table>
      </template>

      <div class="paste">
        <p class="label">或直接粘贴商品 ID（不勾选候选时以这里为准；每行一个，# 开头忽略）</p>
        <el-input v-model="pastedIds" type="textarea" :rows="2" placeholder="684521345678" />
        <el-button type="primary" size="small" :loading="running" @click="submitCandidates">
          采集勾选的 {{ picked.length || parseIds(pastedIds).length }} 件
        </el-button>
      </div>
        </el-collapse-item>
      </el-collapse>

      <!-- ============ 编排结果 ============ -->
      <template v-if="runError">
        <el-divider />
        <el-alert type="error" :title="`这次没跑成：${runError}`" :closable="false" show-icon />
      </template>

      <template v-if="runResult">
        <el-divider content-position="left">本次结果：{{ statusLabel }}</el-divider>

        <el-alert
          v-if="runResult.status === 'ok'"
          type="success"
          :closable="false"
          show-icon
          :title="
            `批次 ${runResult.batch_id}：采集 新增 ${runResult.collected.added} / 去重 ${runResult.collected.skipped}` +
            ` / 失败 ${runResult.collected.failed} / 风控 ${runResult.collected.blocked}` +
            `，其中 ${reviewableCount} 件风险扫描通过且有价 → 去工作区放行；耗时 ${(runResult.elapsed_ms / 1000).toFixed(1)}s`
          "
        >
          <template #default>
            <p class="label">
              系统理解到的需求：{{ planView }}
            </p>
            <p v-for="(r, i) in runResult.searched?.rounds || []" :key="i" class="label">
              {{ roundText(r) }}
            </p>
            <p v-if="runResult.collected.aborted" class="danger">
              已连续撞风控、本批提前中止，剩余 {{ runResult.collected.not_attempted }} 件未尝试。
            </p>
          </template>
        </el-alert>

        <el-alert
          v-else-if="runResult.status === 'needs_clarification'"
          type="warning"
          :closable="false"
          show-icon
          title="这句话还不够开工，请补齐后重跑（后端刻意不猜默认值）"
        >
          <template #default>
            <ul class="asks">
              <li v-for="(q, i) in runResult.needs_clarification" :key="i">{{ q }}</li>
            </ul>
            <p class="label">已理解到的部分：{{ planView }}</p>
          </template>
        </el-alert>

        <el-alert
          v-else-if="runResult.status === 'no_source_account'"
          type="error"
          :closable="false"
          show-icon
          :title="runResult.message"
        >
          <template #default>
            <el-link type="primary" @click="router.push('/alibaba/accounts')">去「1688 货源账号」扫码</el-link>
            <span class="label"> —— 这一步在开浏览器之前就拦下，没消耗检索与模型调用。</span>
          </template>
        </el-alert>

        <el-alert
          v-else
          type="warning"
          :closable="false"
          show-icon
          :title="runResult.message || '检索一件也没拿到'"
        >
          <template #default>
            <p v-for="(r, i) in runResult.searched?.rounds || []" :key="i" class="label">{{ roundText(r) }}</p>
          </template>
        </el-alert>

        <el-table v-if="decidedRows.length" :data="decidedRows" border stripe size="small" class="decided">
          <el-table-column prop="offer_id" label="商品 ID" width="120" />
          <el-table-column label="拟用标题" min-width="200" show-overflow-tooltip>
            <template #default="{ row }">
              <span v-if="row.title">{{ row.title }}</span>
              <span v-else-if="row.copy_source === 'none'" class="label">
                文案未产出（模型那一轮不可用），去工作区重跑
              </span>
              <span v-else class="label">-</span>
            </template>
          </el-table-column>
          <el-table-column label="成本" width="90">
            <template #default="{ row }">{{ formatCent(row.source_price_cent) }}</template>
          </el-table-column>
          <el-table-column label="拟定价" width="150">
            <template #default="{ row }">
              <span :class="{ danger: row.pricing === 'manual' }">{{ formatCent(row.price_cent) }}</span>
              <el-tag v-if="row.pricing" :type="tagType(row.pricing)" size="small">{{ pricingText(row.pricing) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="风险" width="110">
            <template #default="{ row }">
              <el-tag v-if="row.risk_passed" type="success" size="small">通过</el-tag>
              <el-tag v-else-if="row.risk_passed === false" type="danger" size="small">
                拦截 {{ row.risk_blockers.length }} 项
              </el-tag>
              <span v-else class="label">-</span>
            </template>
          </el-table-column>
          <el-table-column label="说明" min-width="160">
            <template #default="{ row }">
              <span v-if="row.error" class="danger">{{ row.error }}</span>
              <span v-else-if="row.skipped" class="label">{{ row.skipped }}</span>
              <span v-else-if="row.confidence" class="label">置信 {{ row.confidence }}</span>
            </template>
          </el-table-column>
        </el-table>

        <template v-if="runResult.status === 'ok'">
          <p class="label next">
            采集到这里就停在「待确认」，本页不放行也不上架。
            <template v-if="runResult.needs_human?.length">
              其中 {{ runResult.needs_human.length }} 件需要人工处理（价格待填或文案有风险）。
            </template>
          </p>
          <el-button type="primary" size="small" @click="router.push('/collect/workspace')">
            去采集工作区逐件核对并放行
          </el-button>
        </template>
      </template>
    </el-card>

    <!-- ============ 定时采集（仅管理员） ============ -->
    <el-card v-if="isAdmin" shadow="never" class="sched">
      <template #header>
        <div class="header">
          <span>定时采集（无人值守只到「待确认清单」，不会自动放行）</span>
          <el-button text @click="loadSchedule">刷新</el-button>
        </div>
      </template>
      <el-alert v-if="scheduleError" type="error" :title="scheduleError" :closable="false" show-icon />
      <div class="toolbar">
        <el-switch
          :model-value="!!schedule?.enabled"
          :loading="saving"
          active-text="开启"
          inactive-text="关闭"
          @change="saveSchedule"
        />
        <span class="label">{{ scheduleText }}</span>
      </div>
      <p class="label">
        计划内容 = 上面「一句话需求」这一句 + 当前选的账号，点开关即写一次。到点执行与本页点「开始采集」
        走的是同一个后端函数；每个货源账号每小时最多开 {{ schedule?.quota?.per_hour ?? "-" }} 次检索浏览器，
        当前余 {{ schedule?.quota?.remaining ?? "（未写死货源账号，运行时才选，不预估）" }} 次。
        Cookie 失效时这一项会连着记失败，那是预期不是坏了。
      </p>
    </el-card>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
}
.label {
  color: var(--el-text-color-secondary);
  font-size: 0.85em;
  margin: 4px 0;
}
.running {
  margin-left: 12px;
}
.warn {
  margin-bottom: 12px;
}
.advanced {
  margin-top: 8px;
}
.advanced :deep(.el-collapse-item__content) {
  padding-bottom: 12px;
}
.paste {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 6px;
  margin-top: 8px;
}
.decided,
.asks {
  margin-top: 10px;
}
.asks {
  padding-left: 20px;
}
.next {
  margin: 10px 0;
}
.danger {
  color: var(--el-color-danger);
  font-weight: 600;
}
code {
  font-family: monospace;
  background: var(--el-fill-color-light);
  padding: 2px 4px;
  border-radius: 2px;
}
</style>
