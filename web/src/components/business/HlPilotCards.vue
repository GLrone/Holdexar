<script setup lang="ts">
import { computed, ref } from 'vue'
import type { PilotFacts } from '@/api/client'
import { useI18n } from '@/locales'
import { normalizeCardsToBlocks } from '@/components/structured/adapters/legacy'
import StructuredRenderer from '@/components/structured/StructuredRenderer.vue'
import type { StructuredBlock } from '@/components/structured/types'
import HlButton from '@/components/ui/HlButton.vue'

const props = withDefaults(
  defineProps<{
    cards: (PilotFacts | StructuredBlock | unknown)[]
    /** 悬停操作层（加引用 / 复制）：对话面传 true，纯展示（组件总览）不传 */
    actions?: boolean
  }>(),
  {
    actions: false,
  },
)

const emit = defineEmits<{
  (e: 'proposal', payload: { pid: string; approve: boolean }): void
  (e: 'quote', text: string): void
  (e: 'copy', text: string): void
}>()

const { t } = useI18n()

// 规整为统一规范的 StructuredBlock 实例列表
const blocks = computed(() => normalizeCardsToBlocks(props.cards || []))

// 弹性展开控制：少于等于 3 张直接呈现，多于 3 张时支持首屏折叠预算
const expanded = ref(false)
const displayedBlocks = computed(() => {
  if (expanded.value || blocks.value.length <= 3) return blocks.value
  return blocks.value.slice(0, 3)
})

function onAction(payload: { actionId: string; block: StructuredBlock }) {
  const data = payload.block.data as Record<string, unknown> | null
  const pid = typeof data?.pid === 'string' ? data.pid : ''
  if (payload.actionId === 'proposal_approve' && pid) {
    emit('proposal', { pid, approve: true })
  } else if (payload.actionId === 'proposal_reject' && pid) {
    emit('proposal', { pid, approve: false })
  }
}

function onQuote(text: string) {
  emit('quote', text)
}

function onCopy(text: string) {
  emit('copy', text)
}
</script>

<template>
  <div v-if="blocks.length > 0" class="hsui-pilot-cards-wrapper">
    <div class="hsui-cards-list">
      <StructuredRenderer
        v-for="b in displayedBlocks"
        :key="b.id"
        :block="b"
        :actions="actions"
        host="pilot-drawer"
        class="hsui-card-item"
        @action="onAction"
        @quote="onQuote"
        @copy="onCopy"
      >
      </StructuredRenderer>
    </div>

    <!-- 超过预算时的展开折叠控制器 -->
    <div v-if="blocks.length > 3" class="hsui-cards-expand-bar">
      <HlButton
        variant="text"
        size="sm"
        class="hsui-cards-expand-btn"
        @click="expanded = !expanded"
      >
        <svg
          class="hsui-expand-icon"
          :class="{ 'is-open': expanded }"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
        >
          <polyline points="6 9 12 15 18 9" />
        </svg>
        <span>
          {{
            expanded
              ? t('structured.cards.collapse')
              : t('structured.cards.expandMore', { n: blocks.length - 3 })
          }}
        </span>
      </HlButton>
    </div>
  </div>
</template>

<style scoped>
.hsui-pilot-cards-wrapper {
  display: flex;
  flex-direction: column;
  gap: 10px;
  width: 100%;
}

.hsui-cards-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  width: 100%;
}

.hsui-card-item {
  width: 100%;
}

.hsui-cards-expand-bar {
  display: flex;
  justify-content: center;
  padding: 4px 0;
}

.hsui-cards-expand-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: var(--text-muted, #8f98a0);
}

.hsui-cards-expand-btn:hover {
  color: var(--accent, #66c0f4);
}

.hsui-expand-icon {
  width: 12px;
  height: 12px;
  transition: transform var(--transition, 0.22s ease);
}

.hsui-expand-icon.is-open {
  transform: rotate(180deg);
}
</style>
