<script setup>
// 采集工作区（设计文档 §3.1 / §8.1）：data/collect 的本地批次台账 + 待确认清单 + 单件四段核对
// 「批量放行」是 DECIDED → MAPPED 的唯一人工闸门；放行后 auto_listing / 行内上架才会发出。
import { computed, onMounted, reactive, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { api } from "../api";
import { accountName, useAccounts } from "../composables/useAccounts";
import { formatCent, errText, poolStatusText, pricingText } from "../utils/format";

const { currentAccountId } = useAccounts();
const showAccountCol = computed(() => currentAccountId.value == null);

const tab = ref("pending");
const loading = ref(false);
const error = ref("");

// ---------------- 待确认清单 ----------------

const pendingStatus = ref("DECIDED");
const pending = ref([]);
const selection = ref([]);

const canApprove = (r) =>
  r.pool_status === "DECIDED" && !!r.goods_id && r.has_decision &&
  r.pricing !== "manual" && r.risk_passed !== false;

// §8.4 节点 5：不可放行的行直接置灰（后端 approve 仍独立校验，前端置灰只是少一次白跑）
const selectableRows = computed(() => pending.value.filter(canApprove));

// 置灰的行到底卡在哪。不写这一条的话"按钮点了没反应"只能靠人自己猜（2026-09-20 用户就问过）
const unselectableReason = computed(() => {
  const rows = pending.value.filter((r) => !canApprove(r));
  const count = (f) => rows.filter(f).length;
  const parts = [];
  const noCopy = count((r) => r.copy_source === "none");
  const blocked = count((r) => r.risk_passed === false && r.copy_source !== "none");
  const manual = count((r) => r.pricing === "manual");
  const undecided = count((r) => !r.has_decision);
  if (noCopy) parts.push(`${noCopy} 件文案没产出（模型当时不可用，decision 里没有标题），点行内「重跑定价」`);
  if (blocked) parts.push(`${blocked} 件风险扫描未过（多为标题超长，点行内「重跑定价」让模型改写）`);
  if (manual) parts.push(`${manual} 件定价是「需人工填价」（没抓到成本价），得先人工填价才能放行`);
  if (undecided) parts.push(`${undecided} 件还没跑定价`);
  return parts.join("；") || "没有满足放行条件的行";
});

function rowSelectable(row) {
  return canApprove(row);
}

async function loadPending() {
  loading.value = true;
  try {
    pending.value = await api.listPending(pendingStatus.value, currentAccountId.value || undefined);
    selection.value = [];
    error.value = "";
  } catch (e) {
    error.value = errText(e);
  } finally {
    loading.value = false;
  }
}

// ---------------- 批次 ----------------

const batches = ref([]);
const batchItems = ref([]);
const activeBatch = ref("");

async function loadBatches() {
  loading.value = true;
  try {
    batches.value = await api.listCollectBatches();
    error.value = "";
  } catch (e) {
    error.value = errText(e);
  } finally {
    loading.value = false;
  }
}

async function openBatch(batchId) {
  activeBatch.value = batchId;
  loading.value = true;
  try {
    const body = await api.getCollectBatch(batchId);
    batchItems.value = body.items || [];
    batchMeta.value = body;
    error.value = "";
  } catch (e) {
    error.value = errText(e);
  } finally {
    loading.value = false;
  }
}

const batchMeta = ref({});

// blocked 件没有 Goods 行，面板只能靠 collect/status 这两处说明"这件为什么没有"
const BLOCKED_REASON = {
  risk_shell: "撞风控：需带 1688 货源账号重采",
  parse_empty: "解析失败：页面改版或解析器失效",
};

// ---------------- 单件详情 ----------------

const drawer = reactive({ visible: false, loading: false, batchId: "", offerId: "", data: null });

async function openDetail(row) {
  const batchId = row.batch_id || activeBatch.value;
  if (!batchId) {
    ElMessage.warning("该件缺 batch_id（本地目录未落齐），无法打开详情");
    return;
  }
  drawer.visible = true;
  drawer.loading = true;
  drawer.batchId = batchId;
  drawer.offerId = row.offer_id || row.offerId;
  drawer.data = null;
  try {
    drawer.data = await api.getCollectOffer(batchId, drawer.offerId);
  } catch (e) {
    ElMessage.error(errText(e));
  } finally {
    drawer.loading = false;
  }
}

const product = computed(() => drawer.data?.product || {});
const collect = computed(() => product.value.collect || {});
const decision = computed(() => product.value.decision || null);
const media = computed(() => product.value.media || {});
const status = computed(() => product.value.status || {});
const scan = computed(() => decision.value?.risk_scan || null);
// 成品文案只应由模型撰写。none = 那一轮没产出来（刻意不回落上游标题）；
// clean-title 只出现在 2026-09-20 改造前的历史行里。
const COPY_SOURCE_LABEL = {
  llm: "模型撰写",
  human: "人工改写",
  none: "未产出（模型当时不可用）",
  "clean-title": "历史行：源标题去噪（已废弃该兜底）",
};
const localImages = computed(() => media.value.local_images || []);
const imageUrl = (name) => api.collectImageUrl(drawer.batchId, drawer.offerId, name.replace(/^images\//, ""));
const imagesLabel = computed(() => "本地图（" + localImages.value.length + " 张，≤9）");
const blockedHint = computed(() =>
  status.value.stage === "blocked"
    ? "抓到的风控/空壳页已留存在 product.json 的 collect.raw_excerpt。" +
      "在「1688 货源账号」扫码绑定后，回到 1688 采集页带上该账号重采同一 offer_id 即可覆盖（本件未入库，不会被去重跳过）。"
    : "",
);

// ---------------- 动作：定价 / 放行 ----------------

const busy = ref(false);

async function runDecide(row, force = false) {
  const batchId = row.batch_id || activeBatch.value;
  if (!batchId) {
    // 死行（Goods 在、product.json 没了）拼出的 URL 是 `/offers//123/decide`，只能吃 404
    ElMessage.warning("该件缺 batch_id（本地工作区目录未落或被清），只能重新采集，这里补跑不了定价");
    return;
  }
  busy.value = true;
  try {
    const res = await api.decideOffer(batchId, row.offer_id, { force });
    const d = res.decision;
    ElMessage[scanOf(d) ? "success" : "warning"](
      `已产出 decision：pricing=${d.pricing} 价 ${d.price_cent ? formatCent(d.price_cent) : "待人工"}，` +
        `文案${d.copy_source === "llm" ? "模型已写" : "未产出"}，` +
        `风险扫描${d.risk_scan?.passed ? "通过" : "未过"}（重写 ${d.risk_rewrite_rounds ?? 0} 轮）`,
    );
    await refresh();
  } catch (e) {
    ElMessage.error(errText(e));
  } finally {
    busy.value = false;
  }
}

const scanOf = (d) => d?.risk_scan?.passed;

async function decide(row) {
  if (row.has_decision) {
    try {
      await ElMessageBox.confirm(
        "该件已有 decision 段（§3.3-2：LLM 只写一次）。重跑会覆盖已产出的定价与文案，确认？",
        "覆盖 decision",
        { confirmButtonText: "重跑并覆盖", cancelButtonText: "取消", type: "warning" },
      );
    } catch {
      return;
    }
    return runDecide(row, true);
  }
  return runDecide(row, false);
}

const approving = ref(false);

async function approveSelected() {
  const ids = selection.value.map((r) => r.goods_id).filter(Boolean);
  if (!ids.length) {
    ElMessage.warning("请先勾选待确认的行（未跑定价的行不可勾选放行）");
    return;
  }
  if (ids.length > 5) {
    ElMessage.warning("单次批量放行上限 5 件");
    return;
  }
  try {
    await ElMessageBox.confirm(
      `放行 ${ids.length} 件：把定价与文案写入商品池并置 MAPPED，之后自动上架/行内上架才会发出。` +
        `风险扫描未过或定价为 manual 的行会被整批拒绝。`,
      "确认放行",
      { confirmButtonText: "放行", cancelButtonText: "取消", type: "warning" },
    );
  } catch {
    return;
  }
  approving.value = true;
  try {
    const res = await api.batchApprove(ids);
    ElMessage.success(`已放行 ${res.applied} 件（goods_id: ${res.approved.join("、")}）`);
    await refresh();
  } catch (e) {
    ElMessage.error(errText(e));
  } finally {
    approving.value = false;
  }
}

function refresh() {
  return tab.value === "pending" ? loadPending() : loadBatches();
}

// reserve-selection 会跨数据源保留勾选，换号后必须显式清空，
// 否则「批量放行」可能把上一个号里勾选的件放行掉。
const pendingTable = ref(null);
watch(currentAccountId, () => {
  selection.value = [];
  pendingTable.value?.clearSelection();
  if (tab.value === "pending") loadPending();
});

onMounted(loadPending);
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="header">
        <span>采集工作区（本地 data/collect）</span>
        <div>
          <el-button text @click="refresh">刷新</el-button>
        </div>
      </div>
    </template>

    <el-alert v-if="error" type="error" :title="'加载失败：' + error" :closable="false" show-icon />

    <el-tabs v-model="tab" @tab-change="refresh">
      <!-- ============ 待确认清单 ============ -->
      <el-tab-pane label="待确认清单" name="pending">
        <div class="toolbar">
          <el-radio-group v-model="pendingStatus" size="small" @change="loadPending">
            <el-radio-button value="DECIDED">已跑定价，待放行</el-radio-button>
            <el-radio-button value="SOURCED">仅采集，待定价</el-radio-button>
          </el-radio-group>
          <span class="spacer"></span>
          <el-tag v-if="selectableRows.length" type="success" size="small">
            {{ selectableRows.length }} 件可放行
          </el-tag>
          <el-button
            type="primary"
            size="small"
            :loading="approving"
            :disabled="!selection.length"
            @click="approveSelected"
          >
            批量放行（≤5）
          </el-button>
        </div>

        <!-- 一件都不可放行时把原因摊开说；原先只是复选框置灰 + 按钮禁用，等于让人猜 -->
        <el-alert
          v-if="pending.length && !selectableRows.length"
          class="blocked-alert"
          type="warning"
          :closable="false"
          show-icon
          :title="`0 件可放行：${unselectableReason}`"
        />

        <el-table
          ref="pendingTable"
          v-loading="loading"
          :data="pending"
          border
          stripe
          row-key="goods_id"
          @selection-change="(rows) => (selection = rows)"
        >
          <template #empty><el-empty v-if="!loading" description="没有待确认的采集件" :image-size="80" /></template>
          <el-table-column type="selection" width="42" :selectable="rowSelectable" reserve-selection />
          <el-table-column v-if="showAccountCol" prop="account_id" label="账号" width="140" :formatter="(r) => accountName(r.account_id)" show-overflow-tooltip />
          <el-table-column prop="offer_id" label="商品 ID" width="120" />
          <el-table-column prop="title_raw" label="1688 原标题" min-width="180" show-overflow-tooltip />
          <el-table-column label="成本" width="90">
            <template #default="{ row }">{{ formatCent(row.source_price_cent) }}</template>
          </el-table-column>
          <el-table-column label="拟定价" width="140">
            <template #default="{ row }">
              <span :class="{ danger: row.pricing === 'manual' }">
                {{ formatCent(row.proposed_price_cent) }}
              </span>
              <el-tag v-if="row.pricing" :type="row.pricing === 'llm' ? 'success' : row.pricing === 'manual' ? 'danger' : 'warning'" size="small">
                {{ pricingText(row.pricing) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="倍率/置信" width="110">
            <template #default="{ row }">
              {{ row.markup_ratio_actual ?? "-" }} / {{ row.confidence ?? "-" }}
            </template>
          </el-table-column>
          <el-table-column label="风险扫描" width="120">
            <template #default="{ row }">
              <el-tag v-if="!row.has_decision" type="info" size="small">未跑</el-tag>
              <el-tag v-else-if="row.risk_passed" type="success" size="small">
                通过（图 {{ row.image_count }}）
              </el-tag>
              <el-tag v-else type="danger" size="small">
                拦截 {{ row.risk_blockers.length }} 项
              </el-tag>
              <el-tag v-if="row.copy_source === 'none'" type="danger" size="small" effect="plain">
                文案空白
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="120">
            <template #default="{ row }">
              <el-tag size="small" :type="row.pool_status === 'DECIDED' ? 'warning' : 'info'">
                {{ poolStatusText(row.pool_status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="150" fixed="right">
            <template #default="{ row }">
              <el-button size="small" text type="primary" :loading="busy" @click="decide(row)">
                {{ row.has_decision ? "重跑定价" : "生成定价文案" }}
              </el-button>
              <el-button size="small" text @click="openDetail(row)">详情</el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-collapse class="rules">
          <el-collapse-item title="哪些行能放行、哪些要先处理？" name="rules">
            <ul class="rules-list">
              <li>还没生成定价的行不能勾选放行，先点行内「生成定价文案」。</li>
              <li>标了「需人工填价」的（没抓到成本价），要先去商品页人工改价才能放行。</li>
              <li>风险扫描没过的行，放行时会整批被拒——点行内「重跑定价」让模型改写。</li>
              <li>标题和描述只由模型撰写；那一次模型不可用时文案留空并标「文案空白」，不会拿 1688 原标题顶上，重跑即可。</li>
            </ul>
          </el-collapse-item>
        </el-collapse>
      </el-tab-pane>

      <!-- ============ 采集批次 ============ -->
      <el-tab-pane label="采集批次" name="batches">
        <el-table v-loading="loading && !batchItems.length" :data="batches" border stripe>
          <template #empty><el-empty v-if="!loading" description="还没有采集批次" :image-size="80" /></template>
          <el-table-column prop="batch_id" label="批次" min-width="200" />
          <el-table-column prop="trigger" label="触发" width="90" />
          <el-table-column prop="instruction" label="自然语言指令" min-width="160" show-overflow-tooltip />
          <el-table-column prop="started_at" label="开始时间" width="180" />
          <el-table-column label="件数" width="200">
            <template #default="{ row }">
              <span v-if="row.count">
                新增 {{ row.count.added }} / 跳过 {{ row.count.skipped }} / 失败 {{ row.count.failed }}
                <span v-if="row.count.blocked" class="danger">/ 风控 {{ row.count.blocked }}</span>
              </span>
              <el-tag v-else type="warning" size="small">未收尾</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag v-if="row.status" :type="row.status === 'done' ? 'success' : row.status === 'blocked' ? 'danger' : 'warning'" size="small">
                {{ { done: "完成", partial: "部分", blocked: "被风控" }[row.status] || row.status }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="100" fixed="right">
            <template #default="{ row }">
              <el-button size="small" text type="primary" @click="openBatch(row.batch_id)">查看</el-button>
            </template>
          </el-table-column>
        </el-table>

        <template v-if="activeBatch">
          <el-divider content-position="left">
            批次 {{ activeBatch }}
            <el-tag v-if="batchMeta.trigger" size="small">{{ batchMeta.trigger }}</el-tag>
            <span v-if="batchMeta.instruction" class="label">「{{ batchMeta.instruction }}」</span>
          </el-divider>
          <el-table v-loading="loading" :data="batchItems" border stripe size="small">
            <el-table-column prop="offer_id" label="商品 ID" width="120" />
            <el-table-column prop="title_raw" label="原标题" min-width="180" show-overflow-tooltip />
            <el-table-column label="stage" width="150">
              <template #default="{ row }">
                <el-tag size="small" :type="row.stage === 'blocked' ? 'danger' : 'info'">
                  {{ row.stage || "-" }}
                </el-tag>
                <span v-if="row.blocked_reason" class="label">{{ BLOCKED_REASON[row.blocked_reason] }}</span>
              </template>
            </el-table-column>
            <el-table-column label="定价" width="150">
              <template #default="{ row }">
                <span :class="{ danger: row.pricing === 'manual' }">
                  {{ formatCent(row.proposed_price_cent) }}
                </span>
                <el-tag v-if="row.pricing" size="small">{{ pricingText(row.pricing) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="风险" width="90">
              <template #default="{ row }">
                <el-tag v-if="!row.has_decision" type="info" size="small">未跑</el-tag>
                <el-tag v-else-if="row.risk_passed" type="success" size="small">通过</el-tag>
                <el-tag v-else type="danger" size="small">拦截</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="图" width="60">
              <template #default="{ row }">{{ row.image_count }}</template>
            </el-table-column>
            <el-table-column label="操作" width="150" fixed="right">
              <template #default="{ row }">
                <el-button
                  size="small"
                  text
                  type="primary"
                  :loading="busy"
                  :disabled="row.stage === 'blocked' || !row.goods_id"
                  @click="decide(row)"
                >
                  {{ row.has_decision ? "重跑定价" : "生成定价文案" }}
                </el-button>
                <el-button size="small" text @click="openDetail({ ...row, batch_id: activeBatch, offer_id: row.offer_id })">
                  详情
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </template>
      </el-tab-pane>
    </el-tabs>

    <!-- ============ 单件详情：product.json 四段 ============ -->
    <el-drawer v-model="drawer.visible" size="640px" :title="`${drawer.batchId} / ${drawer.offerId}`">
      <div v-loading="drawer.loading">
        <template v-if="drawer.data">
          <el-alert
            v-if="status.stage === 'blocked'"
            class="blocked-alert"
            type="error"
            :title="`本件未入库：${BLOCKED_REASON[status.history?.[0]?.reason] || '采集未成功'}`"
            :description="blockedHint"
            :closable="false"
            show-icon
          />
          <el-descriptions title="collect（采集器独占）" :column="1" border size="small">
            <el-descriptions-item label="原标题">{{ collect.title_raw }}</el-descriptions-item>
            <el-descriptions-item label="成本价">
              {{ formatCent(collect.source_price_cent) }}
              <el-tag v-if="!collect.price_available" type="danger" size="small">未解析到价格</el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="类目">{{ collect.category_raw || "-" }}</el-descriptions-item>
            <el-descriptions-item label="规格">
              <code>{{ JSON.stringify(collect.specs || {}) }}</code>
            </el-descriptions-item>
            <el-descriptions-item label="卖家/库存">{{ collect.seller || "-" }} / {{ collect.stock ?? "-" }}</el-descriptions-item>
            <el-descriptions-item label="详情页">
              <a :href="collect.url" target="_blank" rel="noreferrer noopener">{{ collect.url }}</a>
            </el-descriptions-item>
            <el-descriptions-item label="抓取时间">{{ collect.fetched_at }}</el-descriptions-item>
          </el-descriptions>

          <el-divider />
          <template v-if="decision">
            <el-descriptions title="decision（LLM 写一次）" :column="1" border size="small">
              <el-descriptions-item label="定价模式">
                <el-tag :type="decision.pricing === 'llm' ? 'success' : decision.pricing === 'manual' ? 'danger' : 'warning'" size="small">
                  {{ decision.pricing }}
                </el-tag>
                {{ formatCent(decision.price_cent) }}
                <span v-if="decision.orig_price_cent" class="label">
                  划线 {{ formatCent(decision.orig_price_cent) }}
                </span>
              </el-descriptions-item>
              <el-descriptions-item label="delivery / 置信">
                {{ decision.delivery }} / {{ decision.confidence }}
              </el-descriptions-item>
              <el-descriptions-item label="定价理由">{{ decision.reasoning }}</el-descriptions-item>
              <el-descriptions-item label="文案来源">
                <el-tag
                  :type="decision.copy_source === 'llm' ? 'success' : decision.copy_source === 'none' ? 'danger' : 'warning'"
                  size="small"
                >
                  {{ COPY_SOURCE_LABEL[decision.copy_source] || decision.copy_source || "-" }}
                </el-tag>
                <span v-if="decision.copy_error" class="label">{{ decision.copy_error }}</span>
              </el-descriptions-item>
              <el-descriptions-item label="标题候选">
                <ol class="candidates">
                  <li v-for="(t, i) in decision.title_candidates || []" :key="i" :class="{ chosen: i === 0 }">
                    {{ t }}<span v-if="i === 0" class="label">（采用）</span>
                  </li>
                </ol>
              </el-descriptions-item>
              <el-descriptions-item label="描述">
                <pre class="desc">{{ decision.desc }}</pre>
              </el-descriptions-item>
            </el-descriptions>

            <el-alert
              v-if="scan"
              :type="scan.passed ? 'success' : 'error'"
              :title="`风险扫描：${scan.passed ? '通过' : '拦截'}（${scan.blockers?.length || 0} 项拦截 / ${scan.warnings?.length || 0} 项提醒）`"
              :closable="false"
              show-icon
            />
            <ul v-if="scan && !scan.passed" class="hits">
              <li v-for="(b, i) in scan.blockers || []" :key="`b${i}`" class="danger">
                [{{ b.code }}] {{ b.field }} 命中「{{ b.term }}」— {{ b.message }}
                <span v-if="b.replacement" class="label">建议改：{{ b.replacement }}</span>
              </li>
            </ul>
            <ul v-if="scan && scan.warnings?.length" class="hits">
              <li v-for="(w, i) in scan.warnings" :key="`w${i}`">
                [{{ w.code }}] {{ w.field }} 「{{ w.term }}」— {{ w.message }}
              </li>
            </ul>
            <el-alert
              v-if="scan?.not_covered?.length"
              type="warning"
              title="扫描器覆盖不到的红线（需人工确认）"
              :description="scan.not_covered.join('；')"
              :closable="false"
            />
          </template>
          <el-empty v-else description="未跑定价/文案（点列表里的「生成定价文案」）" :image-size="60" />

          <el-divider />
          <el-descriptions title="media / status" :column="1" border size="small">
            <el-descriptions-item :label="imagesLabel">
              <div class="thumbs">
                <el-image
                  v-for="img in localImages"
                  :key="img"
                  :src="imageUrl(img)"
                  :preview-src-list="localImages.map(imageUrl)"
                  fit="cover"
                  class="thumb"
                />
                <span v-if="!localImages.length" class="label">本地无图片（采集时未落盘或失败）</span>
              </div>
            </el-descriptions-item>
            <el-descriptions-item label="stage">{{ status.stage }}</el-descriptions-item>
            <el-descriptions-item label="Goods">
              <template v-if="drawer.data.goods">
                #{{ drawer.data.goods.id }} · {{ drawer.data.goods.pool_status }} ·
                {{ formatCent(drawer.data.goods.price_cent) }}
                <span v-if="drawer.data.goods.external_goods_id">
                  · 闲鱼 {{ drawer.data.goods.external_goods_id }}
                </span>
              </template>
              <span v-else class="label">未入库</span>
            </el-descriptions-item>
            <el-descriptions-item label="历史">
              <ul class="history">
                <li v-for="(h, i) in status.history || []" :key="i">
                  {{ h.stage }} — {{ h.at }}
                  <span v-if="h.reason">（{{ BLOCKED_REASON[h.reason] || h.reason }}）</span>
                </li>
              </ul>
            </el-descriptions-item>
            <el-descriptions-item v-if="collect.raw_excerpt" label="抓到的原始内容">
              <pre class="desc excerpt">{{ collect.raw_excerpt }}</pre>
            </el-descriptions-item>
          </el-descriptions>
        </template>
      </div>
    </el-drawer>
  </el-card>
</template>

<style scoped>
.header { display: flex; justify-content: space-between; align-items: center; }
.toolbar { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
.spacer { flex: 1; }
.label { color: var(--el-text-color-secondary); font-size: 0.85em; }
.danger { color: var(--el-color-danger); font-weight: 600; }
code { font-family: monospace; font-size: 0.9em; word-break: break-all; }
.desc { white-space: pre-wrap; margin: 0; font-family: inherit; }
.excerpt { max-height: 180px; overflow: auto; font-family: monospace; font-size: 0.8em; }
.blocked-alert { margin-bottom: 12px; }
.rules { margin-top: 10px; }
.rules-list { margin: 0; padding-left: 20px; font-size: 0.9em; color: var(--el-text-color-secondary); }
.rules-list li { margin: 4px 0; }
.candidates { margin: 0; padding-left: 20px; }
.candidates .chosen { color: var(--el-color-success); }
.hits { margin: 8px 0; padding-left: 20px; font-size: 0.9em; }
.thumbs { display: flex; flex-wrap: wrap; gap: 6px; }
.thumb { width: 72px; height: 72px; border: 1px solid var(--el-border-color-light); }
.history { margin: 0; padding-left: 18px; font-size: 0.9em; color: var(--el-text-color-secondary); }
</style>
