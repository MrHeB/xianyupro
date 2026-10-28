<script setup>
// 扫码组件（阶段 F2）：二维码生成 → 1s 轮询 → confirmed 后 emit。
// 页面复用：QrLogin.vue（独立页）与 账号页「扫码绑号」弹窗共用，省两份轮询代码。
// platform="alibaba" 走 1688 货源账号那套接口（passport.1688.com），流程一致，
// 多一个 verification_required 终态：1688 会要求二次校验，只能给人工开链接去做。
// Props: size（二维码边长，默认 280）、platform（xianyu | alibaba）；Emits: confirmed(account)、error(msg)
import { computed, defineEmits, defineProps, onBeforeUnmount, ref } from "vue";
import { api } from "../api";
import { errText } from "../utils/format";
import { useAccounts } from "../composables/useAccounts";
import { useAlibabaAccounts } from "../composables/useAlibabaAccounts";

const props = defineProps({
  size: { type: Number, default: 280 },
  platform: { type: String, default: "xianyu" },
});
const emits = defineEmits(["confirmed", "error"]);

const { loadAccounts } = useAccounts();
const { loadAlibabaAccounts } = useAlibabaAccounts();
const generating = ref(false);
const qrImage = ref("");
const sessionId = ref("");
const status = ref(""); // '' | new | scaned | confirmed | expired | error | verification_required
const message = ref("");
const account = ref(null);
let timer = null;
let generation = 0;

const ALIBABA = computed(() => props.platform === "alibaba");
const startSession = () =>
  ALIBABA.value ? api.startAlibabaQrLogin() : api.startQrLogin();
const pollSession = (sid) =>
  ALIBABA.value ? api.pollAlibabaQrLogin(sid) : api.pollQrLogin(sid);

const STATUS_TEXT = computed(() => ({
  new: "等待扫码…",
  scaned: ALIBABA.value ? "已扫码，请在手机上确认登录" : "已扫码，请在手机上确认绑号",
  confirmed: ALIBABA.value ? "登录成功！1688 Cookie 已加密入库" : "绑定成功！账号已入库",
  expired: "二维码已过期，请重新生成",
  error: "查询失败，请重试",
  verification_required: "1688 要求二次校验，请在浏览器里完成后重新扫码",
}));
const STATUS_TYPE = {
  new: "info",
  scaned: "warning",
  confirmed: "success",
  expired: "error",
  error: "error",
  verification_required: "warning",
};
const TERMINAL = ["confirmed", "expired", "error", "verification_required"];

async function start() {
  if (generating.value) return;
  stop();
  const current = generation;
  generating.value = true;
  status.value = "";
  qrImage.value = "";
  sessionId.value = "";
  account.value = null;
  message.value = "";
  try {
    const result = await startSession();
    if (current !== generation) return;
    qrImage.value = result.qr_image;
    sessionId.value = result.session_id;
    status.value = "new";
    timer = setTimeout(() => poll(current), 1000);
  } catch (e) {
    if (current !== generation) return;
    status.value = "error";
    message.value = errText(e);
    emits("error", message.value);
  } finally {
    if (current === generation) generating.value = false;
  }
}

async function poll(current) {
  try {
    const result = await pollSession(sessionId.value);
    if (current !== generation) return;
    status.value = result.status;
    if (result.nickname || result.iframe_redirect_url) account.value = result;
    if (TERMINAL.includes(result.status)) {
      stop();
      if (result.status === "confirmed") {
        // 只刷自己这一侧的账号缓存，闲鱼号列表不会被 1688 扫码误刷
        if (ALIBABA.value) loadAlibabaAccounts(true);
        else loadAccounts(true);
        emits("confirmed", result);
      }
      return;
    }
    timer = setTimeout(() => poll(current), 1000);
  } catch (e) {
    if (current !== generation) return;
    stop();
    status.value = "error";
    message.value = errText(e);
    emits("error", message.value);
  }
}

function stop() {
  generation += 1;
  clearTimeout(timer);
  timer = null;
}

onBeforeUnmount(stop);

defineExpose({ start });
</script>

<template>
  <div class="qr-scanner">
    <el-button type="primary" :loading="generating" @click="start">
      生成扫码二维码
    </el-button>

    <div v-if="qrImage && ['new', 'scaned'].includes(status)" class="qr" :style="{ width: props.size + 24 + 'px' }">
      <el-image :src="qrImage" :style="{ width: props.size + 'px', height: props.size + 'px' }" fit="contain" />
    </div>

    <el-alert
      v-if="status"
      class="status"
      :type="STATUS_TYPE[status] || 'info'"
      :title="STATUS_TEXT[status] || status"
      :closable="false"
      show-icon
    >
      <template v-if="account">（{{ account.nickname }}）</template>
    </el-alert>
    <el-alert v-if="message" class="status" type="error" :title="message" :closable="false" show-icon />
    <p v-if="account?.iframe_redirect_url" class="verify">
      <el-link type="primary" :href="account.iframe_redirect_url" target="_blank" rel="noopener">
        打开二次校验页面
      </el-link>
      <span class="label">完成后再点上面的按钮重新扫码</span>
    </p>
  </div>
</template>

<style scoped>
.qr {
  margin-top: 16px;
  padding: 12px;
  border: 1px solid var(--el-border-color);
  border-radius: 6px;
  display: inline-block;
}
.status { margin-top: 16px; }
.verify { margin: 8px 0 0; font-size: 0.85em; }
.label { color: var(--el-text-color-secondary); margin-left: 8px; }
</style>