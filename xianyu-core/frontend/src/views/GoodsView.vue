<script setup>
// 商品页：列表 + 手动登记 + CSV 导入（品类 B 入池）+ 行内上架（四步发布链路）
import { onMounted, reactive, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import { api } from "../api";
import DataTable from "../components/DataTable.vue";
import { useAccountColumns, useAccounts } from "../composables/useAccounts";
import { formatCent, goodsStatusText, poolStatusText, errText } from "../utils/format";

const router = useRouter();
const { accounts, currentAccountId, loadAccounts } = useAccounts();

const loading = ref(false);
const data = ref([]);
const error = ref("");

const columns = useAccountColumns([
  { prop: "id", label: "ID", width: 70 },
  { prop: "title", label: "标题", minWidth: 200, showOverflowTooltip: true },
  { prop: "category", label: "品类", width: 80, tag: true },
  { prop: "source", label: "来源", width: 100, format: (v) => ({ manual: "手动登记", csv: "CSV导入", "1688": "1688采集" }[v] ?? v) },
  { prop: "price_cent", label: "价格", width: 100, format: formatCent },
  { prop: "status", label: "状态", tag: true, format: goodsStatusText },
  { prop: "pool_status", label: "商品池状态", width: 130, tag: true, format: poolStatusText },
  { prop: "external_goods_id", label: "外部商品 ID" },
]);

async function reload() {
  loading.value = true;
  try {
    data.value = await api.listGoods(null, currentAccountId.value || undefined);
    error.value = "";
  } catch (e) {
    error.value = errText(e);
  } finally {
    loading.value = false;
  }
}

watch(currentAccountId, reload);
onMounted(reload);

// ---------- 手动登记商品（POST /api/goods） ----------

const goodsDialog = reactive({ visible: false, submitting: false });
const goodsForm = reactive({
  account_id: null,
  title: "",
  category: "A",
  price: null,
  description: "",
  image_urls: "",
  external_goods_id: "",
});

function openGoodsDialog() {
  Object.assign(goodsForm, {
    account_id: currentAccountId.value ?? null, title: "", category: "A", price: null,
    description: "", image_urls: "", external_goods_id: "",
  });
  goodsDialog.visible = true;
  loadAccounts();
}

async function submitGoods() {
  if (!goodsForm.account_id || !goodsForm.title.trim() || !goodsForm.price) {
    ElMessage.warning("账号、标题、价格为必填");
    return;
  }
  goodsDialog.submitting = true;
  try {
    const result = await api.createGoods({
      account_id: Number(goodsForm.account_id),
      title: goodsForm.title.trim(),
      category: goodsForm.category,
      price_cent: Math.round(Number(goodsForm.price) * 100),
      description: goodsForm.description.trim() || null,
      image_urls: goodsForm.image_urls.split("\n").map((s) => s.trim()).filter(Boolean),
      external_goods_id: goodsForm.external_goods_id.trim() || null,
    });
    ElMessage.success(
      goodsForm.external_goods_id
        ? `商品「${result.title}」已登记并绑定闲鱼商品（ID ${result.id}）`
        : `商品「${result.title}」已登记（ID ${result.id}），点行内「上架」发布到闲鱼`,
    );
    goodsDialog.visible = false;
    await reload();
  } catch (e) {
    ElMessage.error(errText(e));
  } finally {
    goodsDialog.submitting = false;
  }
}

// ---------- CSV 导入（POST /api/goods/import-csv） ----------

const csvDialog = reactive({ visible: false, submitting: false });
const csvForm = reactive({ account_id: null, markup_rate: 1.3, csv_text: "" });

function openCsvDialog() {
  Object.assign(csvForm, { account_id: currentAccountId.value ?? null, markup_rate: 1.3, csv_text: "" });
  csvDialog.visible = true;
  loadAccounts();
}

async function submitCsv() {
  if (!csvForm.account_id || !csvForm.csv_text.trim()) {
    ElMessage.warning("账号与 CSV 内容为必填");
    return;
  }
  csvDialog.submitting = true;
  try {
    const result = await api.importCsv({
      account_id: Number(csvForm.account_id),
      markup_rate: csvForm.markup_rate,
      csv_text: csvForm.csv_text,
    });
    ElMessage.success(`CSV 导入完成：新增 ${result.added} 条，去重跳过 ${result.skipped} 条（品类 B 入池）`);
    csvDialog.visible = false;
    await reload();
  } catch (e) {
    ElMessage.error(errText(e));
  } finally {
    csvDialog.submitting = false;
  }
}

// ---------- 上架（POST /api/goods/{id}/publish） ----------

const publishing = ref(false);

async function publishGoods(row) {
  try {
    await ElMessageBox.confirm(
      `上架「${row.title}」：走四步链路（传图→类目→地址→提交），约需 30 秒以上；写操作限流 1 次/分钟。`,
      "确认上架",
      { confirmButtonText: "上架", cancelButtonText: "取消", type: "info" },
    );
  } catch {
    return; // 用户取消
  }
  publishing.value = true;
  ElMessage.info("上架中（约 30s+，请勿重复操作）…");
  try {
    const result = await api.publishGoods(row.id);
    ElMessage.success(`上架成功：闲鱼商品 ID ${result.external_goods_id}`);
    await reload();
  } catch (e) {
    ElMessage.error(`上架失败：${errText(e)}`);
  } finally {
    publishing.value = false;
  }
}
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="header">
        <span>商品（A 虚拟自动发货 / B 采集人工履约）</span>
        <div>
          <el-button type="primary" @click="openGoodsDialog">手动登记商品</el-button>
          <el-button @click="openCsvDialog">CSV 导入</el-button>
          <el-button text @click="reload">刷新</el-button>
        </div>
      </div>
    </template>

    <el-alert v-if="error" type="error" :title="'加载失败：' + error" :closable="false" show-icon />
    <DataTable v-else :columns="columns" :data="data" :loading="loading">
      <template #action="{ row }">
        <el-tooltip v-if="row.status === 1" content="已在售" placement="top">
          <el-button size="small" type="info" text disabled>已上架</el-button>
        </el-tooltip>
        <el-tooltip
          v-else-if="row.source === '1688' && ['SOURCED', 'DECIDED'].includes(row.pool_status)"
          content="采集件须先到「采集工作区」跑定价与文案并放行（未过风险扫描的文案不允许发布）"
          placement="top"
        >
          <el-button size="small" type="warning" text @click="router.push('/collect/workspace')">
            去放行
          </el-button>
        </el-tooltip>
        <el-button v-else size="small" type="primary" text :disabled="publishing" @click="publishGoods(row)">
          上架
        </el-button>
      </template>
    </DataTable>

    <!-- 手动登记商品弹窗 -->
    <el-dialog v-model="goodsDialog.visible" title="手动登记商品" width="560px">
      <el-form label-width="110px">
        <el-form-item label="账号" required>
          <el-select v-model="goodsForm.account_id" placeholder="选择账号" style="width: 100%">
            <el-option
              v-for="a in accounts"
              :key="a.id"
              :value="a.id"
              :label="`#${a.id} ${a.nickname || a.external_uid}`"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="标题" required>
          <el-input v-model="goodsForm.title" maxlength="128" placeholder="≤128 字" />
        </el-form-item>
        <el-form-item label="品类">
          <el-radio-group v-model="goodsForm.category">
            <el-radio value="A">A 虚拟（自动发货）</el-radio>
            <el-radio value="B">B 采集（人工履约）</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="价格（元）" required>
          <el-input-number v-model="goodsForm.price" :min="0.01" :precision="2" :controls="false" style="width: 160px" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="goodsForm.description" type="textarea" :rows="3" placeholder="选填；上架时作为商品描述" />
        </el-form-item>
        <el-form-item label="图片 URL">
          <el-input v-model="goodsForm.image_urls" type="textarea" :rows="3" placeholder="每行一个 URL；上架需至少 1 张（首图）" />
        </el-form-item>
        <el-form-item label="闲鱼商品 ID">
          <el-input v-model="goodsForm.external_goods_id" maxlength="64" placeholder="选填：已在闲鱼 App 手动上架则填，系统按它匹配发货" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="goodsDialog.visible = false">取消</el-button>
        <el-button type="primary" :loading="goodsDialog.submitting" @click="submitGoods">登记</el-button>
      </template>
    </el-dialog>

    <!-- CSV 导入弹窗 -->
    <el-dialog v-model="csvDialog.visible" title="CSV 导入（品类 B 商品池）" width="560px">
      <el-form label-width="90px">
        <el-form-item label="账号" required>
          <el-select v-model="csvForm.account_id" placeholder="选择账号" style="width: 100%">
            <el-option
              v-for="a in accounts"
              :key="a.id"
              :value="a.id"
              :label="`#${a.id} ${a.nickname || a.external_uid}`"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="加价倍率">
          <el-input-number v-model="csvForm.markup_rate" :min="0.5" :max="3" :step="0.1" :precision="1" />
        </el-form-item>
        <el-form-item label="CSV 内容" required>
          <el-input
            v-model="csvForm.csv_text"
            type="textarea"
            :rows="10"
            placeholder="列：title,price(元),image_urls(分号分隔)[,description,source_offer_id]&#10;首行可为表头；空行与 # 注释行忽略；同账号同标题去重"
          />
        </el-form-item>
      </el-form>
      <p class="label">导入即转换（标题去噪 + 按倍率加价），入池 pool_status=MAPPED，之后点行内「上架」发布。</p>
      <template #footer>
        <el-button @click="csvDialog.visible = false">取消</el-button>
        <el-button type="primary" :loading="csvDialog.submitting" :disabled="!csvForm.csv_text.trim()" @click="submitCsv">导入</el-button>
      </template>
    </el-dialog>
  </el-card>
</template>

<style scoped>
.header { display: flex; justify-content: space-between; align-items: center; }
.label { color: var(--el-text-color-secondary); font-size: 0.9em; }
</style>
