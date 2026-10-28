// 1688 货源账号下拉：模块级缓存 + 单飞（同 useAccounts 的理由，采集页与货源账号页共用）
import { ref } from "vue";
import { api } from "../api";

const alibabaAccounts = ref([]);
let inflight = null;

export function useAlibabaAccounts() {
  async function loadAlibabaAccounts(force = false) {
    if (alibabaAccounts.value.length && !force) return alibabaAccounts.value;
    if (!inflight) {
      inflight = api
        .listAlibabaAccounts()
        .then((list) => {
          alibabaAccounts.value = list;
          return list;
        })
        .catch(() => {
          alibabaAccounts.value = [];
          return [];
        })
        .finally(() => {
          inflight = null;
        });
    }
    return inflight;
  }
  return { alibabaAccounts, loadAlibabaAccounts };
}
