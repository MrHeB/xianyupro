<script setup>
// 发货记录（GET /api/delivery/records）+ 发货规则配置（POST /api/delivery/rules）
import { computed, onMounted, reactive, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { api } from "../api";
import DataTable from "../components/DataTable.vue";
import { useAccountColumns, useAccounts } from "../composables/useAccounts";
import { errText } from "../utils/format";

const { accounts, currentAccountId, loadAccounts } = useAccounts();

const loading = ref(false);
const data = ref([]);
const error = ref("");

const columns = useAccountColumns([
  { prop: "id", label: "ID", width: 70 },
  { prop: "order_id", label: "订单号" },
  { prop: "delivery_mode", label: "发货方式", tag: true },
  { prop: "status", label: "状态", tag: true },
  { prop: "fail_reason", label: "失败/跳过原因", minWidth: 200, showOverflowTooltip: true },
]);

// ---------- 发货规则（同账号+同商品维度覆盖更新） ----------

const ruleDialog = reactive({ visible: false, submitting: false });
const ruleForm = reactive({
  account_id: null,
  goods_id: null,
  delivery_mode: "kami",
  trigger_on_pay: 1,
  max_delivery_per_day: 200,
});
const allGoodsA = ref([]);
// 商品下拉只跟弹窗里选的账号走，避免把别的号的卡密商品配到当前号上
const goodsOptions = computed(() =>
  ruleForm.account_id == null
    ? allGoodsA.value
    : allGoodsA.value.filter((g) => g.account_id === ruleForm.account_id)
);

async function openRuleDialog() {
  Object.assign(ruleForm, {
    account_id: currentAccountId.value ?? null, goods_id: null, delivery_mode: "kami", trigger_on_pay: 1, max_delivery_per_day: 200,
  });
  ruleDialog.visible = true;
  loadAccounts();
  try {
    allGoodsA.value = await api.listGoods("A");
  } catch {
    allGoodsA.value = [];
  }
}

async function submitRule() {
  if (!ruleForm.account_id) {
    ElMessage.warning("请选择账号");
    return;
  }
  ruleDialog.submitting = true;
  try {
    await api.upsertDeliveryRule({
      ...ruleForm,
      account_id: Number(ruleForm.account_id),
      goods_id: ruleForm.goods_id ?? null,
    });
    ElMessage.success("发货规则已保存（同账号+同商品维度覆盖更新）");
    ruleDialog.visible = false;
  } catch (e) {
    ElMessage.error(errText(e));
  } finally {
    ruleDialog.submitting = false;
  }
}

async function reload() {
  loading.value = true;
  try {
    data.value = await api.listDeliveryRecords(currentAccountId.value || undefined);
    error.value = "";
  } catch (e) {
    error.value = errText(e);
  } finally {
    loading.value = false;
  }
}

// 换了弹窗里的账号后原来选的商品就不属于它了，必须清掉
watch(() => ruleForm.account_id, () => { ruleForm.goods_id = null; });

watch(currentAccountId, reload);
onMounted(reload);
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="header">
        <span>发货记录（unknown 态永不自动重发，人工核实）</span>
        <div>
          <el-button type="primary" text @click="openRuleDialog">配置发货规则</el-button>
          <el-button text @click="reload">刷新</el-button>
        </div>
      </div>
    </template>

    <el-alert v-if="error" type="error" :title="'加载失败：' + error" :closable="false" show-icon />
    <DataTable v-else :columns="columns" :data="data" :loading="loading" />

    <!-- 发货规则弹窗 -->
    <el-dialog v-model="ruleDialog.visible" title="配置发货规则（同账号+同商品覆盖更新）" width="520px">
      <el-form label-width="110px">
        <el-form-item label="账号" required>
          <el-select v-model="ruleForm.account_id" placeholder="选择账号" style="width: 100%">
            <el-option
              v-for="a in accounts"
              :key="a.id"
              :value="a.id"
              :label="`#${a.id} ${a.nickname || a.external_uid}`"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="商品">
          <el-select v-model="ruleForm.goods_id" clearable placeholder="留空 = 账号级默认规则（商品级优先）" style="width: 100%">
            <el-option v-for="g in goodsOptions" :key="g.id" :value="g.id" :label="`#${g.id} ${g.title}`" />
          </el-select>
        </el-form-item>
        <el-form-item label="发货形态">
          <el-select v-model="ruleForm.delivery_mode" style="width: 100%">
            <el-option value="kami" label="卡密（当前仅支持）" />
          </el-select>
        </el-form-item>
        <el-form-item label="付款即发">
          <el-switch v-model="ruleForm.trigger_on_pay" :active-value="1" :inactive-value="0" />
        </el-form-item>
        <el-form-item label="每日上限">
          <el-input-number v-model="ruleForm.max_delivery_per_day" :min="1" :max="10000" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="ruleDialog.visible = false">取消</el-button>
        <el-button type="primary" :loading="ruleDialog.submitting" @click="submitRule">保存</el-button>
      </template>
    </el-dialog>
  </el-card>
</template>

<style scoped>
.header { display: flex; justify-content: space-between; align-items: center; }
</style>
