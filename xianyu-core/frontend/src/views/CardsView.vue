<script setup>
// 卡密库存：组列表 + 新建卡密组 / 批量导入卡密（发货时按 goods_id 找组）
import { onMounted, reactive, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { api } from "../api";
import DataTable from "../components/DataTable.vue";
import { accountName, useAccountColumns, useAccounts } from "../composables/useAccounts";
import { errText } from "../utils/format";

const { currentAccountId } = useAccounts();

const loading = ref(false);
const data = ref([]);
const error = ref("");

const columns = useAccountColumns(
  [
    { prop: "id", label: "ID", width: 70 },
    { prop: "name", label: "卡密组" },
    { prop: "goods_id", label: "绑定商品", width: 90 },
    { prop: "total_count", label: "总量", width: 90 },
    { prop: "used_count", label: "已用", width: 90 },
    { prop: "available", label: "剩余", width: 90, danger: (row) => row.low_stock },
    { prop: "low_stock", label: "库存告警", bool: true, width: 100 },
  ],
  // 未绑商品的组没有归属账号，但也得看得见，否则库存像凭空少了一批
  { format: (id) => (id == null ? "未绑定商品" : accountName(id)) },
);

// ---------- 新建卡密组 / 导入卡密 ----------

const cardDialog = reactive({
  visible: false,
  submitting: false,
  importing: false, // false=新建卡密组 / true=导入卡密
  groupId: null,
  groupName: "",
  form: { name: "", goods_id: null, alert_threshold: 5 },
  lines: "",
});
// 下拉选项：商品（品类 A 虚拟商品可绑卡密组）
const goodsOptions = ref([]);

async function openCardDialog(importing, row) {
  Object.assign(cardDialog, {
    visible: true,
    importing,
    groupId: row?.id ?? null,
    groupName: row?.name ?? "",
    form: { name: "", goods_id: row?.goods_id ?? null, alert_threshold: 5 },
    lines: "",
  });
  if (!goodsOptions.value.length) {
    try {
      goodsOptions.value = await api.listGoods("A", currentAccountId.value || undefined);
    } catch {
      goodsOptions.value = [];
    }
  }
}

async function submitCardDialog() {
  if (cardDialog.submitting) return;
  if (cardDialog.importing ? !cardDialog.lines.trim() : !cardDialog.form.name.trim()) {
    ElMessage.warning(cardDialog.importing ? "请输入卡密内容" : "请输入卡密组名称");
    return;
  }
  cardDialog.submitting = true;
  try {
    if (cardDialog.importing) {
      const result = await api.importCardItems(cardDialog.groupId, cardDialog.lines);
      ElMessage.success(`导入成功：新增 ${result.added} 条，组内共 ${result.total_count} 条`);
    } else {
      const result = await api.createCardGroup(cardDialog.form);
      ElMessage.success(`卡密组「${result.name}」已创建（ID ${result.id}），可继续导入卡密`);
    }
    cardDialog.visible = false;
    await reload();
  } catch (e) {
    ElMessage.error(errText(e));
  } finally {
    cardDialog.submitting = false;
  }
}

async function reload() {
  loading.value = true;
  try {
    data.value = await api.listCardGroups(currentAccountId.value || undefined);
    error.value = "";
  } catch (e) {
    error.value = errText(e);
  } finally {
    loading.value = false;
  }
}

watch(currentAccountId, () => {
  goodsOptions.value = []; // 换号后商品下拉要按新号重取
  reload();
});
onMounted(reload);
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="header">
        <span>卡密库存（品类 A 自动发货按商品绑组取卡）</span>
        <div>
          <el-button type="primary" @click="openCardDialog(false)">新建卡密组</el-button>
          <el-button text @click="reload">刷新</el-button>
        </div>
      </div>
    </template>

    <el-alert v-if="error" type="error" :title="'加载失败：' + error" :closable="false" show-icon />
    <DataTable v-else :columns="columns" :data="data" :loading="loading">
      <template #action="{ row }">
        <el-button size="small" type="primary" text @click="openCardDialog(true, row)">导入卡密</el-button>
      </template>
    </DataTable>

    <!-- 新建卡密组 / 导入卡密 弹窗 -->
    <el-dialog
      v-model="cardDialog.visible"
      :title="cardDialog.importing ? `导入卡密 → ${cardDialog.groupName}` : '新建卡密组'"
      width="520px"
      :close-on-click-modal="!cardDialog.submitting"
      :close-on-press-escape="!cardDialog.submitting"
      :show-close="!cardDialog.submitting"
    >
      <el-form v-if="!cardDialog.importing" label-width="90px">
        <el-form-item label="组名" required>
          <el-input v-model="cardDialog.form.name" maxlength="128" placeholder="如：Steam 充值码-10元" />
        </el-form-item>
        <el-form-item label="绑定商品">
          <el-select v-model="cardDialog.form.goods_id" clearable placeholder="品类 A 虚拟商品（发货时按商品找组）" style="width: 100%">
            <el-option v-for="g in goodsOptions" :key="g.id" :value="g.id" :label="`#${g.id} ${g.title}`" />
          </el-select>
        </el-form-item>
        <el-form-item label="告警阈值">
          <el-input-number v-model="cardDialog.form.alert_threshold" :min="0" />
        </el-form-item>
      </el-form>
      <template v-else>
        <p class="label">每行一条：<code>卡号----卡值</code>（卡值可省略）；空行与 # 注释行忽略；重复卡号自动跳过。</p>
        <el-input v-model="cardDialog.lines" type="textarea" :rows="10" placeholder="ABCD-1234----EFGH-5678&#10;IJKL-9012" />
      </template>
      <template #footer>
        <el-button :disabled="cardDialog.submitting" @click="cardDialog.visible = false">取消</el-button>
        <el-button
          type="primary"
          :loading="cardDialog.submitting"
          :disabled="cardDialog.importing ? !cardDialog.lines.trim() : !cardDialog.form.name.trim()"
          @click="submitCardDialog"
        >
          保存
        </el-button>
      </template>
    </el-dialog>
  </el-card>
</template>

<style scoped>
.header { display: flex; justify-content: space-between; align-items: center; }
.label { color: var(--el-text-color-secondary); font-size: 0.9em; margin: 0 0 8px; }
</style>
