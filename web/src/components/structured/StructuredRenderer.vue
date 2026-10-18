<script setup lang="ts">
import { computed, ref } from 'vue'
import type { StructuredAction, StructuredBlock, StructuredDensity, StructuredHost } from './types'
import { registry } from './registry'
import HlSkeleton from '@/components/ui/HlSkeleton.vue'
import HlEmpty from '@/components/ui/HlEmpty.vue'

const props = withDefaults(
  defineProps<{
    block: StructuredBlock
    density?: StructuredDensity
    host?: StructuredHost
    actions?: boolean
  }>(),
  {
    density: 'standard',
    host: 'pilot-drawer',
    actions: true,
  },
)

const emit = defineEmits<{
  (e: 'action', payload: { actionId: string; block: StructuredBlock }): void
  (e: 'quote', text: string): void
  (e: 'copy', text: string): void
}>()

const rootRef = ref<HTMLElement | null>(null)

const effectiveDensity = computed(() => props.block.density || props.density)

const targetComponent = computed(() => registry.getComponent(props.block.componentId))

function extractInnerText(): string {
  if (!rootRef.value) return ''
  const parts: string[] = []
  for (const child of Array.from(rootRef.value.children)) {
    if (child.classList.contains('hsui-renderer__toolbar')) continue
    const text = (child as HTMLElement).innerText || ''
    if (text.trim()) parts.push(text.trim())
  }
  return parts.join('\n').replace(/\n{3,}/g, '\n\n').trim()
}

function onQuote() {
  const text = extractInnerText()
  if (text) emit('quote', text)
}

function onCopy() {
  const text = extractInnerText()
  if (text) emit('copy', text)
}

function onActionClick(act: StructuredAction) {
  if (act.disabled) return
  if (act.handler) {
    act.handler(props.block)
  }
  emit('action', { actionId: act.id, block: props.block })
}
</script>

<template>
  <div
    ref="rootRef"
    class="hsui-block-container"
    :class="[
      `is-density-${effectiveDensity}`,
      `is-host-${host}`,
      `hsui-component-${block.componentId}`,
    ]"
    :data-block-id="block.id"
  >
    <!-- 卡片头部（标题、来源、数据更新时刻） -->
    <header
      v-if="block.title || block.source || block.updatedAt"
      class="hsui-block-header"
    >
      <div class="hsui-block-header__main">
        <h4 v-if="block.title" class="hsui-block-title">{{ block.title }}</h4>
        <span v-if="block.description" class="hsui-block-desc">{{ block.description }}</span>
      </div>
      <div class="hsui-block-header__meta">
        <span v-if="block.source" class="hsui-block-source">{{ block.source }}</span>
        <span v-if="block.updatedAt" class="hsui-block-time">{{ block.updatedAt.slice(0, 16) }}</span>
      </div>
    </header>

    <!-- 状态门禁：加载态 -->
    <div v-if="block.status === 'loading' || block.status === 'pending'" class="hsui-state-loading">
      <HlSkeleton :rows="3" />
    </div>

    <!-- 状态门禁：错误态 -->
    <div v-else-if="block.status === 'error'" class="hsui-state-error">
      <div class="hsui-state-error__icon">!</div>
      <div class="hsui-state-error__text">{{ block.errorMessage || 'Error' }}</div>
    </div>

    <!-- 状态门禁：空态 -->
    <div v-else-if="block.status === 'empty'" class="hsui-state-empty">
      <HlEmpty :description="block.errorMessage || ''" />
    </div>

    <!-- 正常渲染区域 -->
    <main v-else class="hsui-block-content">
      <component
        :is="targetComponent"
        v-if="targetComponent"
        :data="block.data"
        :density="effectiveDensity"
      />
      <!-- 未知组件安全降级 -->
      <div v-else class="hsui-block-fallback">
        <slot :block="block">
          <pre class="hsui-fallback-json">{{ JSON.stringify(block.data, null, 2) }}</pre>
        </slot>
      </div>
    </main>

    <!-- 悬停操作栏（复制、引用、自定义动作） -->
    <footer v-if="actions" class="hsui-renderer__toolbar">
      <button
        type="button"
        class="hsui-act-btn"
        aria-label="Quote"
        @click.stop="onQuote"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="w-3.5 h-3.5">
          <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
        </svg>
      </button>

      <button
        type="button"
        class="hsui-act-btn"
        aria-label="Copy"
        @click.stop="onCopy"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="w-3.5 h-3.5">
          <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
          <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
        </svg>
      </button>

      <button
        v-for="act in block.actions"
        :key="act.id"
        type="button"
        class="hsui-act-btn"
        :title="act.tooltip || act.label"
        :disabled="act.disabled"
        @click.stop="onActionClick(act)"
      >
        {{ act.label }}
      </button>
    </footer>
  </div>
</template>

<style scoped>
.hsui-block-container {
  position: relative;
  width: 100%;
  display: flex;
  flex-direction: column;
  background: var(--surface-card, #1b2838);
  border: 1px solid var(--border-soft, rgba(102, 192, 244, 0.14));
  border-radius: var(--radius, 10px);
  padding: 12px 14px;
  box-sizing: border-box;
  transition: border-color var(--transition, 0.22s ease), box-shadow var(--transition, 0.22s ease);
}

.hsui-block-container:hover {
  border-color: var(--border-strong, rgba(102, 192, 244, 0.28));
}

.hsui-block-container.is-density-compact {
  padding: 8px 10px;
}

.hsui-block-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--line-1, rgba(255, 255, 255, 0.05));
}

.hsui-block-title {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary, #d9e3ec);
  line-height: 1.3;
}

.hsui-block-desc {
  font-size: 11px;
  color: var(--text-dim, #5a7080);
}

.hsui-block-header__meta {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  color: var(--text-muted, #8f98a0);
}

.hsui-block-content {
  width: 100%;
}

.hsui-state-loading,
.hsui-state-empty {
  padding: 16px 0;
}

.hsui-state-error {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px;
  background: var(--danger-a15, rgba(231, 76, 60, 0.15));
  border-radius: var(--radius-sm, 6px);
  color: var(--danger, #e74c3c);
  font-size: 12px;
}

.hsui-state-error__icon {
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: var(--danger, #e74c3c);
  color: var(--text-on-fill);
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: bold;
}

.hsui-block-fallback {
  overflow-x: auto;
  font-size: 11px;
}

.hsui-fallback-json {
  margin: 0;
  padding: 8px;
  background: var(--surface-inset, rgba(0, 0, 0, 0.3));
  border-radius: var(--radius-sm, 6px);
  color: var(--text-secondary, #c7d5e0);
}

/* 悬停操作栏 */
.hsui-renderer__toolbar {
  position: absolute;
  top: 8px;
  right: 8px;
  display: flex;
  align-items: center;
  gap: 4px;
  opacity: 0;
  pointer-events: none;
  background: var(--surface-float, rgba(15, 20, 25, 0.95));
  padding: 3px 6px;
  border-radius: var(--radius-sm, 6px);
  border: 1px solid var(--border-soft, rgba(102, 192, 244, 0.14));
  box-shadow: var(--shadow-sm, 0 1px 2px rgba(23, 32, 42, 0.06));
  transition: opacity var(--transition, 0.22s ease);
  z-index: 5;
}

.hsui-block-container:hover .hsui-renderer__toolbar {
  opacity: 1;
  pointer-events: auto;
}

.hsui-act-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 3px 6px;
  font-size: 11px;
  color: var(--text-secondary, #c7d5e0);
  background: transparent;
  border: none;
  border-radius: 4px;
  cursor: pointer;
  transition: background var(--transition, 0.22s ease), color var(--transition, 0.22s ease);
}

.hsui-act-btn:hover {
  background: var(--hover-soft, rgba(102, 192, 244, 0.15));
  color: var(--text-primary, #d9e3ec);
}

.hsui-act-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
