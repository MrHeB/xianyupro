// 账号下拉选项 + 「当前账号」全局上下文：模块级缓存 + 单飞（各页面共用，切一次全站跟随）
import { computed, ref } from "vue";
import { api } from "../api";

const accounts = ref([]);
let inflight = null;

const ACCOUNT_KEY = "xy_current_account";

// 顶栏切换器写的就是这个值；null = 全部账号（不过滤）
const currentAccountId = ref(Number(localStorage.getItem(ACCOUNT_KEY)) || null);

export function accountName(id) {
  if (id == null) return "全部账号";
  const a = accounts.value.find((x) => x.id === id);
  return a ? `#${a.id} ${a.nickname || a.external_uid}` : `#${id}（已解绑）`;
}

export function useAccounts() {
  async function loadAccounts(force = false) {
    if (accounts.value.length && !force) return prune(accounts.value);
    if (!inflight) {
      inflight = api
        .listAccounts()
        .then((list) => {
          accounts.value = list;
          return list;
        })
        .catch(() => {
          accounts.value = [];
          return [];
        })
        .finally(() => {
          inflight = null;
        });
    }
    return prune(await inflight);
  }

  // 选中的号被解绑/停用后必须回落成"全部账号"，否则各页只剩空白列表且没人知道为什么
  function prune(list) {
    const id = currentAccountId.value;
    if (id != null && !list.some((a) => a.id === id)) setCurrentAccountId(null);
    return list;
  }

  function setCurrentAccountId(id) {
    currentAccountId.value = id ?? null;
    if (id == null) localStorage.removeItem(ACCOUNT_KEY);
    else localStorage.setItem(ACCOUNT_KEY, String(id));
  }

  return { accounts, loadAccounts, currentAccountId, setCurrentAccountId };
}

// 「账号」列：只有在看"全部账号"时才插到最前面，选定单个号后这一列纯属噪音。
// 各列表页共用，避免每个页面自己写一遍账号名映射。
export function useAccountColumns(baseColumns, { format = accountName, label = "账号" } = {}) {
  const { currentAccountId } = useAccounts();
  return computed(() =>
    currentAccountId.value != null
      ? baseColumns
      : [{ prop: "account_id", label, width: 140, format, showOverflowTooltip: true }, ...baseColumns]
  );
}
