<script setup lang="ts">
import { computed } from 'vue'

export interface HlTableColumn {
  key: string
  label: string
  width?: string
  /**
   * 数字列。声明后由组件统一右对齐 + 等宽数字（`tabular-nums`，纵向小数点对齐），
   * 调用方不必再写 `align` 映射。
   *
   * **为什么是显式声明、不按 key 猜**：列 key 是**展示名**，常与数据字段无关——
   * 本项目那张价格表的列 key 就叫 `native` / `cny` / `save`，按 key 后缀猜必然漏判。
   * 「数字列右对齐」此前只活在 `game-detail/Index.vue` 一处手写的 `PRICE_ALIGN` 里，
   * 新表格全靠自觉重写一遍；现在是组件默认值。
   */
  numeric?: boolean
}

const props = withDefaults(
  defineProps<{
    columns: HlTableColumn[]
    rows: Record<string, unknown>[]
    /** 行点击（未设置时行不显示手型） */
    onRowClick?: (row: Record<string, unknown>, index: number) => void
    /** 行级附加 class（如高亮行；返回值拼到 tr 上） */
    rowClass?: (row: Record<string, unknown>, index: number) => string
    /**
     * 对齐覆盖：`{ [colKey]: 'left'|'right'|'center' }`。
     * 优先级高于 `column.numeric`——「折扣」这类数字列要居中，属于例外而非默认。
     */
    align?: Record<string, string>
  }>(),
  { rows: () => [] },
)

/** 解析后的每列对齐：显式 align 覆盖 > numeric 推断 > 靠左（不输出类） */
const alignMap = computed(() => {
  const m: Record<string, string> = {}
  for (const c of props.columns) {
    const explicit = props.align?.[c.key]
    if (explicit) m[c.key] = explicit
    else if (c.numeric) m[c.key] = 'right'
  }
  return m
})

const alignClass = (key: string) => (alignMap.value[key] ? `is-align-${alignMap.value[key]}` : '')
/** 等宽数字只给数字列：正文用 tabular-nums 会让中英混排的字距变松 */
const isNum = (key: string) => alignMap.value[key] === 'right'
</script>

<template>
  <table class="hl-table">
    <thead>
      <tr>
        <th
          v-for="col in columns"
          :key="col.key"
          :class="[alignClass(col.key), { 'is-num': isNum(col.key) }]"
          :style="col.width ? { width: col.width } : undefined"
        >
          {{ col.label }}
        </th>
      </tr>
    </thead>
    <tbody>
      <tr
        v-for="(row, i) in rows"
        :key="i"
        :class="rowClass?.(row, i)"
        :style="!onRowClick ? 'cursor:default' : ''"
        @click="onRowClick?.(row, i)"
      >
        <td
          v-for="col in columns"
          :key="col.key"
          :class="[alignClass(col.key), { 'is-num': isNum(col.key) }]"
        >
          <slot :name="col.key" :row="row" :index="i">{{ row[col.key] }}</slot>
        </td>
      </tr>
    </tbody>
  </table>
</template>
