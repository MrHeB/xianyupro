<script setup>
// 通用数据表：列配置驱动（从旧 App.vue 迁移的渲染协议），6 个列表页共用。
// 列配置字段：prop/label/width/minWidth/showOverflowTooltip/tag/bool/format/danger(row)
// 行操作列走 #action 插槽（父页面拿 row 自己渲染按钮）。
import { useSlots } from "vue";
import { tagType } from "../utils/format";

defineProps({
  columns: { type: Array, required: true },
  data: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
  actionWidth: { type: [Number, String], default: 140 },
});

const slots = useSlots();
const hasAction = slots.action != null; // 有插槽才渲染操作列
</script>

<template>
  <div>
    <el-table v-loading="loading" :data="data" border stripe>
      <el-table-column
        v-for="col in columns"
        :key="col.prop"
        :prop="col.prop"
        :label="col.label"
        :width="col.width"
        :min-width="col.minWidth"
        :show-overflow-tooltip="col.showOverflowTooltip"
      >
        <template #default="{ row }">
          <el-tag
            v-if="col.tag"
            :type="tagType(col.format ? col.format(row[col.prop]) : row[col.prop])"
            disable-transitions
          >
            {{ col.format ? col.format(row[col.prop]) : row[col.prop] }}
          </el-tag>
          <el-tag
            v-else-if="col.bool"
            :type="row[col.prop] ? 'success' : 'info'"
            disable-transitions
          >
            {{ row[col.prop] ? "是" : "否" }}
          </el-tag>
          <span v-else-if="col.danger" :class="{ 'danger-value': col.danger(row) }">
            {{ col.format ? col.format(row[col.prop]) : row[col.prop] }}
          </span>
          <span v-else>{{ col.format ? col.format(row[col.prop]) : row[col.prop] }}</span>
        </template>
      </el-table-column>

      <el-table-column v-if="hasAction" label="操作" :width="actionWidth" fixed="right">
        <template #default="{ row }">
          <slot name="action" :row="row" />
        </template>
      </el-table-column>
      <template #empty>
        <el-empty v-if="!loading" description="暂无记录" :image-size="80" />
        <span v-else>加载中…</span>
      </template>
    </el-table>
  </div>
</template>

<style scoped>
.danger-value {
  color: var(--el-color-danger);
  font-weight: 600;
}
</style>
