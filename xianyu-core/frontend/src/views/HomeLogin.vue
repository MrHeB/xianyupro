<script setup>
// 系统登录页（首页 /）：输入用户名密码登录；默认管理员账号由后端 seed（见 .env JWT 部分）。
// 注册开关默认收起，开放注册仅供小规模试用；正式运营可在后端关掉 /auth/register。
import { ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { api } from "../api";
import { errText } from "../utils/format";

const router = useRouter();
const username = ref("");
const password = ref("");
const loading = ref(false);
const showRegister = ref(false);
const regPassword = ref("");
const regUsername = ref("");

async function doLogin() {
  if (!username.value || !password.value) {
    ElMessage.warning("请输入用户名和密码");
    return;
  }
  loading.value = true;
  try {
    await api.login(username.value.trim(), password.value);
    ElMessage.success("登录成功");
    router.push("/dashboard");
  } catch (e) {
    ElMessage.error(errText(e));
  } finally {
    loading.value = false;
  }
}

async function doRegister() {
  if (!regUsername.value || !regPassword.value) {
    ElMessage.warning("请输入注册用户名和密码");
    return;
  }
  try {
    await api.register({ username: regUsername.value.trim(), password: regPassword.value });
    ElMessage.success("注册成功，请登录");
    showRegister.value = false;
    username.value = regUsername.value.trim();
  } catch (e) {
    ElMessage.error(errText(e));
  }
}
</script>

<template>
  <div class="login-page">
    <el-card class="login-card" shadow="always">
      <div class="brand">
        <div class="brand-title">xianyu-core</div>
        <div class="brand-sub">闲鱼自动化管理系统</div>
      </div>

      <el-form label-position="top" @submit.prevent>
        <el-form-item label="用户名">
          <el-input v-model="username" placeholder="admin" @keyup.enter="doLogin" />
        </el-form-item>
        <el-form-item label="密码">
          <el-input v-model="password" type="password" show-password placeholder="••••••••" @keyup.enter="doLogin" />
        </el-form-item>
        <el-button type="primary" class="submit" :loading="loading" @click="doLogin">
          登 录
        </el-button>
      </el-form>

      <div class="opt">
        <el-button text type="primary" size="small" @click="showRegister = !showRegister">
          {{ showRegister ? "收起注册" : "注册新账号" }}
        </el-button>
      </div>

      <el-collapse-transition>
        <el-form v-if="showRegister" label-position="top" @submit.prevent>
          <el-form-item label="注册用户名（3-64 位字母/数字/._-）">
            <el-input v-model="regUsername" />
          </el-form-item>
          <el-form-item label="密码（至少 8 位）">
            <el-input v-model="regPassword" type="password" show-password />
          </el-form-item>
          <el-button class="submit" @click="doRegister">注 册</el-button>
        </el-form>
      </el-collapse-transition>
    </el-card>
  </div>
</template>

<style scoped>
.login-page {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, var(--el-fill-color-lighter), var(--el-border-color-extra-light));
}
.login-card { width: 360px; }
.brand { text-align: center; margin-bottom: 20px; }
.brand-title { font-size: 22px; font-weight: 700; letter-spacing: 1px; }
.brand-sub { font-size: 12px; color: var(--el-text-color-secondary); margin-top: 4px; }
.submit { width: 100%; }
.opt { text-align: center; margin-top: 8px; }
</style>