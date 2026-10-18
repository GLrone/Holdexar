<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from '@/locales'
import type { DataTableData, StructuredDensity, TableColumnDef } from '../types'
import HlTable, { type HlTableColumn } from '@/components/ui/HlTable.vue'
import HlPagination from '@/components/ui/HlPagination.vue'
import HlEmpty from '@/components/ui/HlEmpty.vue'
import HlButton from '@/components/ui/HlButton.vue'

const props = withDefaults(
  defineProps<{
    data: DataTableData
    density?: StructuredDensity
  }>(),
  { density: 'standard' },
)

const emit = defineEmits<{
  (e: 'row-click', row: Record<string, unknown>): void
  (e: 'action', payload: { actionId: string; row: Record<string, unknown> }): void
}>()

const { t } = useI18n()

const sortCol = ref<string | null>(null)
const sortOrder = ref<'asc' | 'desc'>('asc')
const currentPage = ref(props.data?.currentPage || 1)
const pageSize = computed(() => props.data?.pageSize || 10)

const hlColumns = computed<HlTableColumn[]>(() => {
  const cols: HlTableColumn[] = (props.data?.columns || []).map((c) => ({
    key: c.prop,
    label: c.label,
    width: c.width,
    numeric: c.numeric,
  }))
  if (props.data?.rowActions?.length) {
    cols.push({
      key: '__actions',
      label: t('structured.dataTable.actions'),
      width: '100px',
      numeric: false,
    })
  }
  return cols
})

const alignMap = computed(() => {
  const m: Record<string, string> = {}
  for (const c of props.data?.columns || []) {
    if (c.align) m[c.prop] = c.align
  }
  return m
})

const processedRows = computed(() => {
  const list = [...(props.data?.rows || [])]
  if (sortCol.value) {
    const key = sortCol.value
    const asc = sortOrder.value === 'asc'
    list.sort((a, b) => {
      const va = a[key]
      const vb = b[key]
      if (va === vb) return 0
      if (va == null) return asc ? -1 : 1
      if (vb == null) return asc ? 1 : -1
      return asc ? (va > vb ? 1 : -1) : (va < vb ? 1 : -1)
    })
  }
  return list
})

const paginatedRows = computed(() => {
  const list = processedRows.value
  const size = pageSize.value
  const start = (currentPage.value - 1) * size
  return list.slice(start, start + size)
})

const total = computed(() => props.data?.total ?? processedRows.value.length)

function onRowClick(row: Record<string, unknown>) {
  emit('row-click', row)
}

function handleRowAction(actionId: string, row: Record<string, unknown>) {
  emit('action', { actionId, row })
}
</script>

<template>
  <div class="hsui-data-table" :class="`is-density-${density}`">
    <div v-if="!data?.rows || data.rows.length === 0" class="hsui-data-table__empty">
      <HlEmpty :description="data?.emptyText || t('structured.dataTable.empty')" />
    </div>

    <div v-else class="hsui-data-table__wrapper">
      <HlTable
        :columns="hlColumns"
        :rows="paginatedRows"
        :align="alignMap"
        :on-row-click="onRowClick"
        class="hsui-data-table__table"
      >
        <template v-for="col in data.columns" :key="col.prop" #[col.prop]="{ row }">
          <span
            v-if="col.badgeTone && col.badgeTone(row[col.prop], row)"
            class="hsui-table-badge"
            :class="`is-${col.badgeTone(row[col.prop], row)}`"
          >
            {{ col.formatter ? col.formatter(row[col.prop], row) : row[col.prop] }}
          </span>
          <span
            v-else
            :class="{
              'hl-num': col.numeric,
              'is-highlight': col.highlight,
            }"
          >
            {{ col.formatter ? col.formatter(row[col.prop], row) : (row[col.prop] ?? '—') }}
          </span>
        </template>

        <template #__actions="{ row }">
          <div class="hsui-table-actions">
            <HlButton
              v-for="act in data.rowActions"
              :key="act.id"
              size="sm"
              variant="text"
              @click.stop="handleRowAction(act.id, row)"
            >
              {{ act.label }}
            </HlButton>
          </div>
        </template>
      </HlTable>

      <div v-if="total > pageSize" class="hsui-data-table__pagination">
        <HlPagination
          v-model="currentPage"
          :total="total"
          :page-size="pageSize"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
.hsui-data-table {
  width: 100%;
  display: flex;
  flex-direction: column;
}

.hsui-data-table__empty {
  padding: 24px 0;
}

.hsui-data-table__wrapper {
  width: 100%;
  overflow-x: auto;
}

.hsui-data-table__table {
  width: 100%;
}

.hsui-data-table.is-density-compact :deep(th),
.hsui-data-table.is-density-compact :deep(td) {
  padding-top: 6px;
  padding-bottom: 6px;
  font-size: 12px;
}

.hsui-table-badge {
  display: inline-flex;
  align-items: center;
  padding: 2px 8px;
  border-radius: var(--radius-sm, 6px);
  font-size: 11px;
  font-weight: 500;
  line-height: 1.2;
}

.hsui-table-badge.is-good {
  background: var(--success-a15, rgba(164, 208, 7, 0.15));
  color: var(--success, #a4d007);
}

.hsui-table-badge.is-warn {
  background: var(--warning-a40, rgba(245, 158, 11, 0.2));
  color: var(--warning, #f59e0b);
}

.hsui-table-badge.is-bad {
  background: var(--danger-a15, rgba(231, 76, 60, 0.15));
  color: var(--danger, #e74c3c);
}

.hsui-table-badge.is-info {
  background: var(--accent-a15, rgba(102, 192, 244, 0.15));
  color: var(--accent, #66c0f4);
}

.hsui-table-actions {
  display: flex;
  align-items: center;
  gap: 4px;
}

.hsui-data-table__pagination {
  margin-top: 10px;
  display: flex;
  justify-content: flex-end;
}
</style>
