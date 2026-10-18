<script setup lang="ts">
import { computed } from 'vue'
import { useI18n, type MessageKey } from '@/locales'
import type { CoverageMatrixData, MatrixCell, MatrixRow, StructuredDensity } from '../types'
import HlTooltip from '@/components/ui/HlTooltip.vue'
import RegionFlag from '@/components/RegionFlag.vue'
import HlEmpty from '@/components/ui/HlEmpty.vue'

const props = withDefaults(
  defineProps<{
    data: CoverageMatrixData
    density?: StructuredDensity
  }>(),
  { density: 'standard' },
)

const emit = defineEmits<{
  (e: 'cell-click', payload: { row: MatrixRow; colId: string; cell: MatrixCell }): void
  (e: 'row-click', row: MatrixRow): void
}>()

const { t } = useI18n()

const columns = computed(() => props.data?.columns ?? [])
const rows = computed(() => props.data?.rows ?? [])
const stats = computed(() => props.data?.stats)

function getCell(row: MatrixRow, colId: string): MatrixCell | undefined {
  return row.cells?.[colId]
}

function cellStatusText(status?: string): string {
  const map: Record<string, MessageKey> = {
    ok: 'structured.coverageMatrix.status.ok',
    failed: 'structured.coverageMatrix.status.failed',
    not_attempted: 'structured.coverageMatrix.status.notAttempted',
    expired: 'structured.coverageMatrix.status.expired',
    locked: 'structured.coverageMatrix.status.locked',
  }
  const key = status ? map[status] : undefined
  return key ? t(key) : t('structured.coverageMatrix.status.unknown')
}

function onCellClick(row: MatrixRow, colId: string, cell?: MatrixCell) {
  if (cell) {
    emit('cell-click', { row, colId, cell })
  }
}
</script>

<template>
  <div class="hsui-coverage-matrix" :class="`is-density-${density}`">
    <!-- 头部统计摘要 -->
    <div v-if="stats" class="hsui-matrix-stats">
      <div class="hsui-matrix-stats__item">
        <span class="hsui-matrix-stats__k">{{ t('structured.coverageMatrix.rate') }}</span>
        <span class="hsui-matrix-stats__v hl-num" :class="stats.coverageRate >= 80 ? 'is-good' : 'is-warn'">
          {{ Math.round(stats.coverageRate) }}%
        </span>
      </div>
      <div class="hsui-matrix-stats__item">
        <span class="hsui-matrix-stats__k">{{ t('structured.coverageMatrix.okUnits') }}</span>
        <span class="hsui-matrix-stats__v hl-num is-good">{{ stats.okUnits }} / {{ stats.totalUnits }}</span>
      </div>
      <div v-if="stats.failedUnits > 0" class="hsui-matrix-stats__item">
        <span class="hsui-matrix-stats__k">{{ t('structured.coverageMatrix.failedUnits') }}</span>
        <span class="hsui-matrix-stats__v hl-num is-bad">{{ stats.failedUnits }}</span>
      </div>
    </div>

    <div v-if="rows.length === 0" class="hsui-matrix-empty">
      <HlEmpty :description="t('structured.coverageMatrix.empty')" />
    </div>

    <!-- 滚动矩阵表格 -->
    <div v-else class="hsui-matrix-scroll">
      <table class="hsui-matrix-table">
        <thead>
          <tr>
            <th class="hsui-matrix-th--sticky">{{ t('structured.coverageMatrix.object') }}</th>
            <th
              v-for="col in columns"
              :key="col.id"
              class="hsui-matrix-th"
              :title="col.label"
            >
              <div class="hsui-matrix-th__inner">
                <RegionFlag v-if="col.id.length === 2" :code="col.id" />
                <span class="hsui-matrix-th__text">{{ col.label }}</span>
              </div>
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.rowId" class="hsui-matrix-tr">
            <td class="hsui-matrix-td--sticky" @click="emit('row-click', row)">
              <div class="hsui-matrix-row-info">
                <span class="hsui-matrix-row-title" :title="row.name">{{ row.name }}</span>
                <span v-if="row.subText" class="hsui-matrix-row-sub">{{ row.subText }}</span>
              </div>
            </td>

            <td
              v-for="col in columns"
              :key="col.id"
              class="hsui-matrix-td"
            >
              <HlTooltip
                v-if="getCell(row, col.id)"
                :content="`${col.label}: ${cellStatusText(getCell(row, col.id)?.status)} ${getCell(row, col.id)?.reason ? '(' + getCell(row, col.id)?.reason + ')' : ''}`"
              >
                <button
                  type="button"
                  class="hsui-matrix-cell-btn"
                  :class="`is-${getCell(row, col.id)?.status || 'unknown'}`"
                  @click="onCellClick(row, col.id, getCell(row, col.id))"
                >
                  <span class="hsui-matrix-dot"></span>
                </button>
              </HlTooltip>
              <span v-else class="hsui-matrix-cell--empty">—</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.hsui-coverage-matrix {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.hsui-matrix-stats {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 8px 12px;
  background: var(--surface-card, #1b2838);
  border: 1px solid var(--border-soft, rgba(102, 192, 244, 0.14));
  border-radius: var(--radius, 10px);
  font-size: 12px;
}

.hsui-matrix-stats__item {
  display: flex;
  align-items: center;
  gap: 6px;
}

.hsui-matrix-stats__k {
  color: var(--text-muted, #8f98a0);
}

.hsui-matrix-stats__v {
  font-weight: 700;
  color: var(--text-primary, #d9e3ec);
}

.hsui-matrix-stats__v.is-good {
  color: var(--success, #a4d007);
}

.hsui-matrix-stats__v.is-warn {
  color: var(--warning, #f59e0b);
}

.hsui-matrix-stats__v.is-bad {
  color: var(--danger, #e74c3c);
}

.hsui-matrix-scroll {
  width: 100%;
  overflow-x: auto;
  border: 1px solid var(--border-soft, rgba(102, 192, 244, 0.14));
  border-radius: var(--radius, 10px);
  background: var(--surface-card, #1b2838);
}

.hsui-matrix-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
  text-align: center;
}

.hsui-matrix-th,
.hsui-matrix-td {
  padding: 8px 10px;
  border-bottom: 1px solid var(--border-soft, rgba(102, 192, 244, 0.08));
  border-right: 1px solid var(--border-soft, rgba(102, 192, 244, 0.05));
  white-space: nowrap;
}

.hsui-matrix-th--sticky,
.hsui-matrix-td--sticky {
  position: sticky;
  left: 0;
  background: var(--surface-card, #1b2838);
  z-index: 2;
  text-align: left;
  border-right: 1px solid var(--border-strong, rgba(102, 192, 244, 0.28));
  min-width: 130px;
  max-width: 180px;
}

.hsui-matrix-th {
  color: var(--text-muted, #8f98a0);
  font-weight: 500;
  background: var(--surface-card-2, #2a475e);
}

.hsui-matrix-th__inner {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.hsui-matrix-row-info {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.hsui-matrix-row-title {
  color: var(--text-primary, #d9e3ec);
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.hsui-matrix-row-sub {
  font-size: 11px;
  color: var(--text-dim, #5a7080);
}

.hsui-matrix-cell-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  border-radius: 50%;
  border: none;
  background: transparent;
  cursor: pointer;
  padding: 0;
  transition: transform var(--transition, 0.22s ease);
}

.hsui-matrix-cell-btn:hover {
  transform: scale(1.2);
}

.hsui-matrix-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
}

.hsui-matrix-cell-btn.is-ok .hsui-matrix-dot {
  background: var(--success, #a4d007);
  box-shadow: 0 0 6px var(--success-a40, rgba(164, 208, 7, 0.4));
}

.hsui-matrix-cell-btn.is-failed .hsui-matrix-dot {
  background: var(--danger, #e74c3c);
  box-shadow: 0 0 6px var(--danger-a40, rgba(231, 76, 60, 0.4));
}

.hsui-matrix-cell-btn.is-not_attempted .hsui-matrix-dot {
  background: var(--surface-track, rgba(255, 255, 255, 0.12));
}

.hsui-matrix-cell-btn.is-expired .hsui-matrix-dot {
  background: var(--warning, #f59e0b);
}

.hsui-matrix-cell-btn.is-locked .hsui-matrix-dot {
  background: var(--text-dim, #5a7080);
}

.hsui-matrix-cell--empty {
  color: var(--text-dim, #5a7080);
}

.hsui-matrix-empty {
  padding: 24px 0;
}
</style>
