<script setup>
// 管理后台布局壳：左侧分组菜单（可折叠）+ 顶栏（当前页标题）+ router-view
// 登录页（meta.blank）走无菜单/无顶栏的居中模式
import { computed, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import {
  Odometer,
  Monitor,
  ChatDotRound,
  Tickets,
  Van,
  Box,
  FolderChecked,
  Key,
  User,
  Camera,
  Setting,
  Shop,
  Fold,
  Expand,
} from "@element-plus/icons-vue";
import { api, clearAuth, getUserMeta } from "../api";
import { useAccounts } from "../composables/useAccounts";

const route = useRoute();
const router = useRouter();
const collapsed = ref(false);
const isBlank = computed(() => route.meta.blank === true);
const user = computed(() => getUserMeta());

// 全局「当前账号」：只在按账号看的列表页出现（切一次全站跟随）。
// 闲鱼账号 / 1688 货源账号 / LLM 配置这三页本身与"当前哪个号"无关，露出来就是个死控件。
const { accounts, currentAccountId, setCurrentAccountId, loadAccounts } = useAccounts();
const SWITCH_PATHS = new Set([
  "/dashboard",
  "/goods",
  "/cards",
  "/messages",
  "/orders",
  "/delivery",
  "/collect/workspace",
  "/collect/1688",
]);
const showSwitch = computed(() => SWITCH_PATHS.has(route.path));
watch(isBlank, (blank) => !blank && loadAccounts(), { immediate: true });

// 菜单分组（顺序即展示顺序）按真实链路排：货源凭据 → 采集 → 人工放行 → 发布 → 交易。
// 这里只留入口，扫码动作一律收在各自账号页的行内按钮里——侧栏曾有独立「扫码绑号」页，
// 而账号页行内已有同一个 QrScanner 弹窗，两个入口还共用 Camera 图标，是最容易点错的地方。
const menuGroups = [
  {
    label: "总览",
    items: [
      { path: "/dashboard", title: "工作台", icon: Odometer },
      { path: "/live", title: "事件流（调试）", icon: Monitor },
    ],
  },
  {
    label: "货源",
    items: [
      { path: "/alibaba/accounts", title: "1688 货源账号", icon: Shop },
      { path: "/collect/1688", title: "1688 采集", icon: Camera },
      { path: "/collect/workspace", title: "采集工作区", icon: FolderChecked },
    ],
  },
  {
    label: "发布",
    items: [
      { path: "/accounts", title: "闲鱼账号", icon: User },
      { path: "/goods", title: "商品", icon: Box },
      { path: "/cards", title: "卡密库存", icon: Key },
    ],
  },
  {
    label: "交易",
    items: [
      { path: "/messages", title: "消息", icon: ChatDotRound },
      { path: "/orders", title: "订单", icon: Tickets },
      { path: "/delivery", title: "发货记录", icon: Van },
    ],
  },
  {
    label: "设置",
    items: [{ path: "/settings/llm", title: "LLM 配置", icon: Setting }],
  },
];

// 面包屑的分组取自菜单本身，不再在路由 meta 里维护第二份组名（那份会和侧栏漂移）。
const crumb = computed(() => {
  for (const g of menuGroups) {
    const hit = g.items.find((i) => i.path === route.path);
    if (hit) return [g.label, hit.title];
  }
  return [route.meta.title || ""];
});

function logout() {
  api.logout().catch(() => {});
  clearAuth();
  ElMessage.success("已退出登录");
  router.push("/");
}
</script>

<template>
  <!-- 登录页等 blank 页面：无菜单无顶栏，登录卡片居中 -->
  <div v-if="isBlank" class="layout blank">
    <router-view />
  </div>

  <el-container v-else class="layout">
    <el-aside :width="collapsed ? '64px' : '200px'" class="aside">
      <div class="brand">
        <span v-if="!collapsed" class="brand-name">xianyu-core</span>
        <span v-else class="brand-name">x</span>
      </div>
      <el-scrollbar>
        <el-menu :default-active="route.path" router :collapse="collapsed" :collapse-transition="false">
          <!-- prop 是 title 不是 label：写成 :label 时分组标题根本不渲染，侧栏就退化成一条平铺清单 -->
          <el-menu-item-group v-for="group in menuGroups" :key="group.label" :title="collapsed ? '' : group.label">
            <el-menu-item v-for="item in group.items" :key="item.path" :index="item.path">
              <el-icon><component :is="item.icon" /></el-icon>
              <template #title>{{ item.title }}</template>
            </el-menu-item>
          </el-menu-item-group>
        </el-menu>
      </el-scrollbar>
    </el-aside>

    <el-container>
      <el-header class="header">
        <el-button text :aria-label="collapsed ? '展开菜单' : '折叠菜单'" :aria-expanded="!collapsed" @click="collapsed = !collapsed">
          <el-icon :size="18"><component :is="collapsed ? Expand : Fold" /></el-icon>
        </el-button>
        <el-breadcrumb separator="/" class="crumb">
          <el-breadcrumb-item v-for="(part, i) in crumb" :key="part" :class="{ 'crumb-page': i === crumb.length - 1 }">
            {{ part }}
          </el-breadcrumb-item>
        </el-breadcrumb>
        <span class="spacer"></span>
        <el-tooltip
          v-if="showSwitch"
          content="切一次，商品/消息/订单/发货/采集各页同时只看这个号；选「全部账号」看混合列表"
          placement="bottom"
        >
          <div class="acct-switch" :class="{ 'is-on': currentAccountId != null }">
            <span class="acct-switch-label">当前账号</span>
            <el-select
              :model-value="currentAccountId ?? 0"
              size="small"
              class="acct-switch-select"
              @update:model-value="(v) => setCurrentAccountId(v || null)"
            >
              <!-- 0 而不是 null：el-select 把 null 当成"没选"，会退回 placeholder -->
              <el-option :value="0" label="全部账号" />
              <el-option
                v-for="a in accounts"
                :key="a.id"
                :value="a.id"
                :label="`#${a.id} ${a.nickname || a.external_uid}`"
              />
            </el-select>
          </div>
        </el-tooltip>
        <span v-if="user.username" class="user-chip">
          {{ user.username }}<span v-if="user.role" class="user-role">{{ user.role === "admin" ? "管理员" : "用户" }}</span>
        </span>
        <el-button text size="small" @click="logout">退出</el-button>
      </el-header>
      <el-main class="main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<style>
/* 全局：布局根全高（从旧 App.vue 迁来） */
html, body, #app { height: 100%; margin: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: var(--el-text-color-primary); }
.el-dialog { max-width: calc(100vw - 32px); }
@media (max-width: 768px) {
  .main { --el-main-padding: 12px; }
  .main .header, .main .toolbar { flex-wrap: wrap; gap: 12px; }
  .main .header > div { display: flex; flex-wrap: wrap; gap: 8px; }
  .main .header > div .el-button { margin-left: 0; }
}
</style>

<style scoped>
.layout { height: 100vh; height: 100dvh; }
.blank { display: flex; align-items: center; justify-content: center; }
.spacer { flex: 1; }
.user-chip { font-size: 13px; color: var(--el-text-color-regular); margin-right: 8px; }
.acct-switch {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-right: 12px;
  padding: 2px 8px;
  border: 1px solid var(--el-border-color-light);
  border-radius: 14px;
}
/* 选中单个号时高亮：让"现在只看一个号"这件事在页面上永远看得见，不会误判成全量数据 */
.acct-switch.is-on {
  border-color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}
.acct-switch-label { font-size: 12px; color: var(--el-text-color-secondary); }
.acct-switch.is-on .acct-switch-label { color: var(--el-color-primary); }
.acct-switch-select { width: 190px; }
.user-role {
  margin-left: 6px;
  padding: 1px 8px;
  font-size: 12px;
  border-radius: 10px;
  background: var(--el-fill-color-light);
  color: var(--el-text-color-secondary);
}
.layout > .el-container { min-width: 0; }
.aside {
  border-right: 1px solid var(--el-border-color-light);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  transition: width 0.2s;
}
.brand {
  height: 56px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 600;
  font-size: 15px;
  border-bottom: 1px solid var(--el-border-color-light);
  white-space: nowrap;
}
.aside .el-menu {
  border-right: none;
}
.header {
  display: flex;
  align-items: center;
  gap: 8px;
  border-bottom: 1px solid var(--el-border-color-light);
  height: 56px;
}
.crumb {
  font-size: 15px;
  white-space: nowrap;
  overflow: hidden;
}
.crumb :deep(.el-breadcrumb__separator) {
  margin: 0 6px;
  color: var(--el-text-color-placeholder);
}
.crumb-page :deep(.el-breadcrumb__inner) {
  font-weight: 600;
  color: var(--el-text-color-primary);
}
.main {
  background: var(--el-fill-color-lighter);
  overflow: auto;
}
</style>
