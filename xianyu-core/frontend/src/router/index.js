// 路由表：/ 为系统登录页；其余页面均在 MainLayout 内（登录后访问）。
// 守卫：未登录只能停留在 /；已登录访问 / 自动进工作台 /dashboard。
import { createRouter, createWebHistory } from "vue-router";
import { getToken } from "../api";

// 顺序与侧栏分组一致（MainLayout.menuGroups）；分组只在那一处维护，
// meta 里不再放 group——那份没人读，且已经和侧栏文案漂移过一次。
const routes = [
  {
    path: "/",
    component: () => import("../views/HomeLogin.vue"),
    meta: { title: "系统登录", public: true, blank: true },
  },
  {
    path: "/dashboard",
    component: () => import("../views/Dashboard.vue"),
    meta: { title: "工作台" },
  },
  {
    path: "/live",
    component: () => import("../views/LiveMessages.vue"),
    meta: { title: "事件流（调试）" },
  },
  {
    path: "/alibaba/accounts",
    component: () => import("../views/AlibabaAccountsView.vue"),
    meta: { title: "1688 货源账号" },
  },
  {
    path: "/collect/1688",
    component: () => import("../views/Collect1688View.vue"),
    meta: { title: "1688 采集" },
  },
  {
    path: "/collect/workspace",
    component: () => import("../views/CollectWorkspaceView.vue"),
    meta: { title: "采集工作区" },
  },
  {
    path: "/accounts",
    component: () => import("../views/AccountsView.vue"),
    meta: { title: "闲鱼账号" },
  },
  {
    path: "/goods",
    component: () => import("../views/GoodsView.vue"),
    meta: { title: "商品" },
  },
  {
    path: "/cards",
    component: () => import("../views/CardsView.vue"),
    meta: { title: "卡密库存" },
  },
  {
    path: "/messages",
    component: () => import("../views/MessagesView.vue"),
    meta: { title: "消息" },
  },
  {
    path: "/orders",
    component: () => import("../views/OrdersView.vue"),
    meta: { title: "订单" },
  },
  {
    path: "/delivery",
    component: () => import("../views/DeliveryView.vue"),
    meta: { title: "发货记录" },
  },
  {
    path: "/settings/llm",
    component: () => import("../views/LlmConfigView.vue"),
    meta: { title: "LLM 配置" },
  },
  // 闲鱼扫码已收在「闲鱼账号」行内按钮里，本页只做深链保留、不在侧栏出现。
  {
    path: "/login",
    component: () => import("../views/QrLogin.vue"),
    meta: { title: "扫码绑号（闲鱼）" },
  },
];

export const router = createRouter({
  history: createWebHistory(),
  routes,
});

router.beforeEach((to) => {
  const authed = !!getToken();
  if (!authed) {
    // 未登录：只能在系统登录页
    if (to.path !== "/") return "/";
    return true;
  }
  // 已登录：直接进工作台
  if (to.path === "/") return "/dashboard";
  return true;
});