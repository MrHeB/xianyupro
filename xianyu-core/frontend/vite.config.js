import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// 第一阶段做薄：纯调后端 API，零业务逻辑
// 后端地址默认 localhost:8000；VITE_BACKEND 可临时指向另一端口（本地烟测/多实例）
const backend = process.env.VITE_BACKEND || "http://localhost:8000";

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    // 开发时代理到后端
    proxy: {
      "/api": backend,
      "/ws": { target: backend.replace(/^http/, "ws"), ws: true },
    },
  },
});
