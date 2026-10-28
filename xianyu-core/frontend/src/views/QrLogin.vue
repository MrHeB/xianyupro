<script setup>
// 扫码绑号页（阶段 4-1 auth / F2）：核心轮询逻辑已抽到 QrScanner 组件复用。
// 语义说明：这里是「闲鱼账号绑定到当前登录用户」，不是系统登录。
import { ref } from "vue";
import QrScanner from "../components/QrScanner.vue";

const confirmed = ref(null);
</script>

<template>
  <el-card class="qr-login" shadow="never">
    <template #header>闲鱼扫码绑号（Cookie 加密入库 + 绑到当前登录用户）</template>
    <QrScanner @confirmed="confirmed = $event" />
    <p v-if="confirmed" class="hint">已可在左侧菜单「账号」页查看</p>
  </el-card>
</template>

<style scoped>
.qr-login { max-width: 420px; }
.hint { color: var(--el-text-color-secondary); font-size: 0.85em; margin-top: 8px; }
</style>