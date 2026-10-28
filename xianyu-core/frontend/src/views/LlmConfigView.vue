<script setup>
// LLM 配置页（阶段 F3）：配置存 DB（app_setting 表），覆盖 .env，保存即生效免重启。
// 角色：admin 可读写+测试；user 只读（隐藏保存/测试按钮）。
import { onMounted, ref } from "vue";
import { ElMessage } from "element-plus";
import { api, getUserMeta } from "../api";
import { errText } from "../utils/format";

const isAdmin = ref(getUserMeta().role === "admin");
// 工作时段后端存的是整数小时（0-23），不是 "HH:MM"
const HOURS = Array.from({ length: 24 }, (_, i) => i);
const loading = ref(false);
const saving = ref(false);
const testing = ref(false);
const error = ref("");
const llm = ref(null); // 占位：避免空引用
const testResult = ref(null);
const form = ref({
  api_key: "",
  base_url: "",
  model: "",
  reply_mode: "auto",
  max_daily_replies: 200,
  blocked_keywords: "",
  work_hour_start: null,
  work_hour_end: null,
  history_max_messages: 10,
  max_message_age_seconds: 600,
  system_prompt: "",
});

async function load() {
  loading.value = true;
  error.value = "";
  try {
    const cfg = await api.getLlmConfig();
    form.value.api_key = "";
    form.value.base_url = cfg.base_url || "";
    form.value.model = cfg.model || "";
    form.value.reply_mode = cfg.reply_mode || "auto";
    form.value.max_daily_replies = cfg.max_daily_replies ?? 200;
    form.value.blocked_keywords = cfg.blocked_keywords || "";
    form.value.work_hour_start = cfg.work_hour_start ?? null;
    form.value.work_hour_end = cfg.work_hour_end ?? null;
    form.value.history_max_messages = cfg.history_max_messages ?? 10;
    form.value.max_message_age_seconds = cfg.max_message_age_seconds ?? 600;
    form.value.system_prompt = cfg.system_prompt || "";
    llm.value = cfg; // 保留脱敏后的 token 信息展示
  } catch (e) {
    error.value = errText(e);
  } finally {
    loading.value = false;
  }
}

async function save() {
  saving.value = true;
  try {
    const body = {
      // 后端约定：字段缺省 = 不改，空串/None = 清除 DB 覆盖回退 .env。
      // 所以"要能清空"的字段必须显式发值，只有 api_key 例外（留空 = 沿用已存 Key）。
      base_url: form.value.base_url || undefined,
      model: form.value.model || undefined,
      api_key: form.value.api_key || undefined,
      reply_mode: form.value.reply_mode,
      max_daily_replies: form.value.max_daily_replies,
      blocked_keywords: form.value.blocked_keywords,
      work_hour_start: form.value.work_hour_start ?? null,
      work_hour_end: form.value.work_hour_end ?? null,
      history_max_messages: form.value.history_max_messages,
      max_message_age_seconds: form.value.max_message_age_seconds,
      system_prompt: form.value.system_prompt,
    };
    await api.updateLlmConfig(body);
    // 保存后重读一遍：清空某字段时后端会回退到 .env 默认值，表单得显示"真正生效的那份"，
    // 否则框里是空的而实际还在拦四个词，下次进来还以为没存上。
    await load();
    ElMessage.success("已保存，AI 引擎已免重启生效");
  } catch (e) {
    ElMessage.error(errText(e));
  } finally {
    saving.value = false;
  }
}

async function test() {
  testing.value = true;
  testResult.value = null;
  try {
    testResult.value = await api.testLlm();
  } catch (e) {
    testResult.value = { ok: false, error: errText(e) };
  } finally {
    testing.value = false;
  }
}

onMounted(load);
</script>

<template>
  <div v-loading="loading" class="wrap">
    <el-alert
      v-if="error"
      type="error"
      :title="'加载失败：' + error"
      :closable="false"
      show-icon
    />
    <el-card v-else shadow="never" header="LLM 配置（DB 覆盖 .env，免重启生效）">
      <el-form label-width="130px" label-position="left">
        <el-form-item label="API Key">
          <el-input
            v-model="form.api_key"
            type="password"
            show-password
            placeholder="留空表示沿用已保存/环境变量中的 Key"
          />
          <span v-if="llm && llm.api_key_set" class="status">
            <el-tag size="small" type="success" effect="plain">已配置</el-tag>
          </span>
        </el-form-item>
        <el-form-item label="API 地址 (Base URL)">
          <el-input v-model="form.base_url" placeholder="如 https://api.openai.com/v1" />
        </el-form-item>
        <el-form-item label="模型 (Model)">
          <el-input v-model="form.model" placeholder="如 gpt-4o-mini / deepseek-chat" />
        </el-form-item>
        <el-form-item label="回复模式">
          <el-radio-group v-model="form.reply_mode" :disabled="!isAdmin">
            <el-radio value="auto">自动</el-radio>
            <el-radio value="hybrid">混合</el-radio>
            <el-radio value="manual">手动</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="每日最大回复数">
          <el-input-number
            v-model="form.max_daily_replies"
            :min="1"
            :max="10000"
            :disabled="!isAdmin"
          />
        </el-form-item>
        <el-form-item label="屏蔽关键词">
          <el-input
            v-model="form.blocked_keywords"
            placeholder="逗号分隔，如：转人工,投诉,发票"
            :disabled="!isAdmin"
          />
          <span class="hint">买家消息命中即不自动回复（按正则片段匹配）；清空 = 回退默认关键词</span>
        </el-form-item>
        <el-form-item label="工作时间">
          <el-select
            v-model="form.work_hour_start"
            clearable
            placeholder="不限（全天）"
            style="width: 130px"
            :disabled="!isAdmin"
          >
            <el-option v-for="h in HOURS" :key="`s${h}`" :value="h" :label="`${h} 点`" />
          </el-select>
          <span class="sep">至</span>
          <el-select
            v-model="form.work_hour_end"
            clearable
            placeholder="不限（全天）"
            style="width: 130px"
            :disabled="!isAdmin"
          >
            <el-option v-for="h in HOURS" :key="`e${h}`" :value="h" :label="`${h} 点`" />
          </el-select>
          <!-- 后端 policy 取的是 now.hour，只到整点；这里必须是数字，之前用 el-time-select 交的是 "09:00" 字符串 -->
          <span class="hint">含两端、按整点判定，开始大于结束即跨夜（如 22 → 6）；任一侧留空 = 全天</span>
        </el-form-item>
        <el-form-item label="对话历史条数">
          <el-input-number
            v-model="form.history_max_messages"
            :min="1"
            :max="50"
            :disabled="!isAdmin"
          />
        </el-form-item>
        <el-form-item label="旧消息时限（秒）">
          <el-input-number
            v-model="form.max_message_age_seconds"
            :min="0"
            :max="86400"
            :disabled="!isAdmin"
          />
        </el-form-item>
        <el-form-item label="系统人设 Prompt">
          <el-input
            v-model="form.system_prompt"
            type="textarea"
            :rows="5"
            placeholder="可留空以使用默认人设"
            :disabled="!isAdmin"
          />
        </el-form-item>
        <el-form-item>
          <el-button
            v-if="isAdmin"
            type="primary"
            :loading="saving"
            :disabled="testing"
            @click="save"
          >
            保存（立即生效）
          </el-button>
          <el-button
            v-if="isAdmin"
            :loading="testing"
            :disabled="saving"
            @click="test"
          >
            测试连接
          </el-button>
          <el-tag v-if="llm" class="status" type="info" effect="plain">
            当前模型：{{ llm.model || "(未配置)" }}
          </el-tag>
        </el-form-item>
        <el-alert
          v-if="testResult"
          class="test-result"
          :type="testResult.ok ? 'success' : 'error'"
          :title="testResult.ok ? '连接成功' : '连接失败'"
          :description="testResult.error || testResult.detail"
          :closable="false"
          show-icon
        />
        <p v-if="!isAdmin" class="readonly-hint">当前为只读用户，请联系管理员配置。</p>
      </el-form>
    </el-card>
  </div>
</template>

<style scoped>
.wrap { max-width: 720px; }
.status { margin-left: 8px; }
.sep { margin: 0 8px; color: var(--el-text-color-secondary); }
/* el-form-item 内容是 flex，不占满一行就会挤在控件右边 */
.hint {
  display: block;
  width: 100%;
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--el-text-color-secondary);
}
.test-result { margin-top: 12px; }
.readonly-hint {
  margin-top: 8px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
