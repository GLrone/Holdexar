<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from '@/locales'
import type { StatusBoardData, StatusBoardItem, StructuredDensity } from '../types'
import HlButton from '@/components/ui/HlButton.vue'
import HlEmpty from '@/components/ui/HlEmpty.vue'

const props = withDefaults(
  defineProps<{
    data: StatusBoardData
    density?: StructuredDensity
  }>(),
  { density: 'standard' },
)

const emit = defineEmits<{
  (e: 'item-action', item: StatusBoardItem): void
}>()

const { t } = useI18n()
const groups = computed(() => props.data?.groups ?? [])

function statusClass(status: string): string {
  switch (status) {
    case 'online':
    case 'healthy':
    case 'idle':
      return 'is-good'
    case 'warn':
    case 'warning':
    case 'degraded':
      return 'is-warn'
    case 'offline':
    case 'critical':
      return 'is-bad'
    case 'busy':
      return 'is-busy'
    default:
      return 'is-neutral'
  }
}

function groupBadgeText(status?: string): string {
  if (status === 'healthy') return t('structured.statusBoard.healthy')
  if (status === 'warning') return t('structured.statusBoard.warning')
  return t('structured.statusBoard.critical')
}

function handleAction(item: StatusBoardItem) {
  if (item.onAction) {
    item.onAction()
  }
  emit('item-action', item)
}
</script>

<template>
  <div class="hsui-status-board" :class="`is-density-${density}`">
    <!-- 顶部总览条 -->
    <div v-if="data?.summaryText || data?.overallStatus" class="hsui-status-board__summary">
      <div class="hsui-status-board__indicator" :class="statusClass(data.overallStatus || 'healthy')">
        <span class="hsui-status-board__dot"></span>
        <span class="hsui-status-board__summary-text">{{ data.summaryText || t('structured.statusBoard.allHealthy') }}</span>
      </div>
    </div>

    <div v-if="groups.length === 0" class="hsui-status-board__empty">
      <HlEmpty :description="t('structured.statusBoard.empty')" />
    </div>

    <!-- 分组展示 -->
    <div v-else class="hsui-status-board__groups">
      <div
        v-for="group in groups"
        :key="group.id"
        class="hsui-status-group"
      >
        <div class="hsui-status-group__header">
          <span class="hsui-status-group__title">{{ group.title }}</span>
          <span
            v-if="group.status"
            class="hsui-status-group__badge"
            :class="statusClass(group.status)"
          >
            {{ groupBadgeText(group.status) }}
          </span>
        </div>

        <div class="hsui-status-group__items">
          <div
            v-for="item in group.items"
            :key="item.id"
            class="hsui-status-item"
            :class="statusClass(item.status)"
          >
            <div class="hsui-status-item__main">
              <div class="hsui-status-item__head">
                <span class="hsui-status-item__dot"></span>
                <span class="hsui-status-item__name">{{ item.name }}</span>
                <span v-if="item.statusText" class="hsui-status-item__state-text">{{ item.statusText }}</span>
              </div>
              <div v-if="item.subTitle" class="hsui-status-item__sub">
                {{ item.subTitle }}
              </div>
            </div>

            <!-- 指标组 -->
            <div v-if="item.metrics?.length" class="hsui-status-item__metrics">
              <div
                v-for="(m, mIdx) in item.metrics"
                :key="mIdx"
                class="hsui-status-item__metric"
              >
                <span class="hsui-status-item__metric-k">{{ m.label }}</span>
                <span class="hsui-status-item__metric-v hl-num">{{ m.value }}</span>
              </div>
            </div>

            <!-- 操作按钮 -->
            <div v-if="item.actionLabel" class="hsui-status-item__action">
              <HlButton size="sm" variant="text" @click="handleAction(item)">
                {{ item.actionLabel }}
              </HlButton>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.hsui-status-board {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.hsui-status-board__summary {
  display: flex;
  align-items: center;
  padding: 8px 12px;
  background: var(--surface-card, #1b2838);
  border: 1px solid var(--border-soft, rgba(102, 192, 244, 0.14));
  border-radius: var(--radius, 10px);
}

.hsui-status-board__indicator {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  font-weight: 600;
}

.hsui-status-board__dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
}

.hsui-status-board__indicator.is-good .hsui-status-board__dot {
  background: var(--success, #a4d007);
  box-shadow: 0 0 6px var(--success-a40, rgba(164, 208, 7, 0.4));
}

.hsui-status-board__indicator.is-warn .hsui-status-board__dot {
  background: var(--warning, #f59e0b);
  box-shadow: 0 0 6px var(--warning-a40, rgba(245, 158, 11, 0.4));
}

.hsui-status-board__indicator.is-bad .hsui-status-board__dot {
  background: var(--danger, #e74c3c);
  box-shadow: 0 0 6px var(--danger-a40, rgba(231, 76, 60, 0.4));
}

.hsui-status-board__summary-text {
  color: var(--text-primary, #d9e3ec);
}

.hsui-status-board__groups {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.hsui-status-group {
  background: var(--surface-card, #1b2838);
  border: 1px solid var(--border-soft, rgba(102, 192, 244, 0.14));
  border-radius: var(--radius, 10px);
  padding: 12px 14px;
}

.hsui-status-group__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}

.hsui-status-group__title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary, #d9e3ec);
}

.hsui-status-group__badge {
  font-size: 11px;
  padding: 2px 6px;
  border-radius: var(--radius-sm, 6px);
  font-weight: 500;
}

.hsui-status-group__badge.is-good {
  background: var(--success-a15, rgba(164, 208, 7, 0.15));
  color: var(--success, #a4d007);
}

.hsui-status-group__badge.is-warn {
  background: var(--warning-a40, rgba(245, 158, 11, 0.2));
  color: var(--warning, #f59e0b);
}

.hsui-status-group__badge.is-bad {
  background: var(--danger-a15, rgba(231, 76, 60, 0.15));
  color: var(--danger, #e74c3c);
}

.hsui-status-group__items {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.hsui-status-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px;
  padding: 8px 10px;
  border-radius: var(--radius-sm, 6px);
  background: var(--surface-inset, rgba(0, 0, 0, 0.2));
  border: 1px solid var(--border-soft, rgba(102, 192, 244, 0.08));
}

.hsui-status-item__main {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 120px;
}

.hsui-status-item__head {
  display: flex;
  align-items: center;
  gap: 6px;
}

.hsui-status-item__dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex-shrink: 0;
}

.hsui-status-item.is-good .hsui-status-item__dot {
  background: var(--success, #a4d007);
}

.hsui-status-item.is-warn .hsui-status-item__dot {
  background: var(--warning, #f59e0b);
}

.hsui-status-item.is-bad .hsui-status-item__dot {
  background: var(--danger, #e74c3c);
}

.hsui-status-item.is-busy .hsui-status-item__dot {
  background: var(--accent, #66c0f4);
}

.hsui-status-item__name {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-primary, #d9e3ec);
}

.hsui-status-item__state-text {
  font-size: 11px;
  color: var(--text-muted, #8f98a0);
}

.hsui-status-item__sub {
  font-size: 11px;
  color: var(--text-dim, #5a7080);
}

.hsui-status-item__metrics {
  display: flex;
  align-items: center;
  gap: 12px;
}

.hsui-status-item__metric {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
}

.hsui-status-item__metric-k {
  font-size: 10px;
  color: var(--text-dim, #5a7080);
}

.hsui-status-item__metric-v {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary, #c7d5e0);
}

.hsui-status-board__empty {
  padding: 24px 0;
}
</style>
