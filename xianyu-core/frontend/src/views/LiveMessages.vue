<script setup>
// 事件流（调试）：WS 订阅事件总线，实时看原始事件（买家消息/付款/登录失效）。
// 面向排障——日常看消息和开关自动回复请到「消息」页。
import { onBeforeUnmount, onMounted, ref } from "vue";

let disposed = false;

const events = ref([]); // 最近 200 条事件（新的在上）
const wsState = ref("connecting"); // connecting | open | closed
let ws = null;
let reconnectTimer = null;

const TYPE_TEXT = {
  message_received: "买家消息",
  order_paid: "已付款",
  order_created: "新订单(人工履约)",
  cookie_expired: "登录失效",
  heartbeat: "心跳",
};

function connect() {
  if (disposed) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws/messages`);
  wsState.value = "connecting";

  ws.onopen = () => (wsState.value = "open");
  ws.onclose = () => {
    wsState.value = "closed";
    if (!disposed) reconnectTimer = setTimeout(connect, 3000);
  };
  ws.onmessage = (msg) => {
    let data;
    try {
      data = JSON.parse(msg.data);
    } catch {
      return;
    }
    if (data.type === "heartbeat") return; // 心跳不上屏
    const p = data.payload || {};
    events.value.unshift({
      key: Date.now() + Math.random(),
      at: new Date(data.occurred_at || Date.now()).toLocaleTimeString(),
      type: data.type,
      account: data.account_id,
      sender: p.senderUserId || p.buyerUserId || "-",
      name: p.senderUserName || p.buyerName || "",
      contentType: p.contentType,
      content: String(p.msgContent || p.goodsTitle || "").slice(0, 120),
      goods: p.xyGoodsId || "",
    });
    if (events.value.length > 200) events.value.length = 200;
  };
}

onMounted(connect);

onBeforeUnmount(() => {
  disposed = true;
  clearTimeout(reconnectTimer);
  if (ws) {
    ws.onopen = null;
    ws.onclose = null;
    ws.onmessage = null;
    ws.close();
  }
});
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="header">
        <span>事件流（调试用：实时原始事件；看聊天记录、开关自动回复请去「消息」页）</span>
        <el-tag :type="wsState === 'open' ? 'success' : wsState === 'connecting' ? 'warning' : 'danger'">
          {{ wsState === 'open' ? '已连接' : wsState === 'connecting' ? '连接中' : '已断开（自动重连）' }}
        </el-tag>
      </div>
    </template>

    <el-table :data="events" border stripe height="560">
      <el-table-column prop="at" label="时间" width="100" />
      <el-table-column label="事件" width="110">
        <template #default="{ row }">
          <el-tag :type="row.type === 'order_paid' ? 'success' : row.type === 'order_created' ? 'warning' : row.type === 'cookie_expired' ? 'danger' : 'primary'" disable-transitions>
            {{ TYPE_TEXT[row.type] || row.type }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="account" label="账号" width="70" />
      <el-table-column label="对方" width="150">
        <template #default="{ row }">
          <span>{{ row.name || row.sender }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="contentType" label="类型" width="60" />
      <el-table-column prop="content" label="内容" min-width="260" show-overflow-tooltip />
      <el-table-column prop="goods" label="商品ID" width="140" show-overflow-tooltip />
    </el-table>

    <el-empty v-if="!events.length" description="暂无事件：扫码登录后，用另一个账号给本账号发消息试试" />
  </el-card>
</template>

<style scoped>
.header { display: flex; justify-content: space-between; align-items: center; }
</style>
