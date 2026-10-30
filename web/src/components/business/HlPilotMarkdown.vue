<template>
  <div class="pilot-md" :class="{ 'is-inline': inline }" v-html="html" />
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from '@/locales'
import { mdLiteInlineToHtml, mdLiteToHtml } from '@/lib/markdownLite'

const props = withDefaults(defineProps<{
  /** 原始 Markdown 源文本（唯一权威内容，渲染只改展示） */
  source: string
  /** 行内模式：带引用胶囊的消息片段渲染（块级结构让位胶囊流） */
  inline?: boolean
}>(), { inline: false })

const { t } = useI18n()

/* 源文本不变即不重解析：历史消息在流式回合里零重复解析成本 */
const html = computed(() => (props.inline
  ? mdLiteInlineToHtml(props.source)
  : mdLiteToHtml(props.source, {
      svgPendingLabel: t('pilot.svgPending'),
      svgDrawingLabel: t('pilot.svgDrawing'),
      svgPreviewLabel: t('pilot.svgPreview'),
      svgCodeLabel: t('pilot.svgCode'),
      svgCopyLabel: t('pilot.svgCopy'),
      svgQuoteLabel: t('pilot.svgQuote'),
    })))
</script>

<style scoped>
.pilot-md {
  margin: 0;
  font-size: 13px;
  line-height: 1.75;
  color: var(--text-primary);
  word-break: break-word;
}

/* 行内模式：带引用胶囊的消息片段参与句内排版，块级结构让位胶囊流 */
.pilot-md.is-inline {
  display: inline;
}

.pilot-md :deep(p) {
  margin: 0 0 6px;
}

.pilot-md :deep(p:last-child) {
  margin-bottom: 0;
}

.pilot-md :deep(strong) {
  font-weight: 600;
}

.pilot-md :deep(code) {
  padding: 1px 5px;
  border-radius: 4px;
  background: var(--accent-a10);
  font-size: 12px;
}

.pilot-md :deep(ul),
.pilot-md :deep(ol) {
  margin: 2px 0 6px;
  padding-left: 18px;
  display: grid;
  gap: 2px;
}

.pilot-md :deep(li)::marker {
  color: var(--text-secondary);
}

.pilot-md :deep(pre) {
  margin: 4px 0 8px;
  padding: 10px 12px;
  border: 1px solid var(--border-soft);
  border-radius: 8px;
  background: var(--bg-card);
  overflow-x: auto;
}

.pilot-md :deep(pre code) {
  padding: 0;
  background: none;
  border-radius: 0;
  font-size: 12px;
  line-height: 1.6;
}

.pilot-md :deep(.pilot-svg) {
  margin: 8px 0 12px;
  border: 1px solid var(--border-soft);
  border-radius: 10px;
  background: var(--bg-card);
  overflow: hidden;
}

.pilot-md :deep(.pilot-svg-toolbar) {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 10px;
  border-bottom: 1px solid var(--border-soft);
  background: var(--bg-hover);
  font-size: 11px;
}

.pilot-md :deep(.pilot-svg-badge) {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
  color: var(--text-secondary);
  font-size: 11px;
}

.pilot-md :deep(.pilot-svg-badge svg) {
  width: 12px;
  height: 12px;
  color: var(--accent);
}

.pilot-md :deep(.pilot-svg-actions) {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.pilot-md :deep(.pilot-svg-tab) {
  padding: 2px 8px;
  border: 1px solid transparent;
  border-radius: 5px;
  background: none;
  color: var(--text-secondary);
  font-size: 11px;
  cursor: pointer;
  transition: all calc(var(--duration-1) * var(--motion-scale)) ease;
}

.pilot-md :deep(.pilot-svg-tab:hover) {
  color: var(--text-primary);
  background: var(--bg-card);
}

.pilot-md :deep(.pilot-svg-tab.is-active) {
  color: var(--accent);
  background: var(--bg-card);
  border-color: var(--border-soft);
  font-weight: 600;
}

.pilot-md :deep(.pilot-svg-quote),
.pilot-md :deep(.pilot-svg-copy) {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  padding: 0;
  border: none;
  border-radius: 5px;
  background: none;
  color: var(--text-secondary);
  cursor: pointer;
  transition: all calc(var(--duration-1) * var(--motion-scale)) ease;
}

.pilot-md :deep(.pilot-svg-quote svg),
.pilot-md :deep(.pilot-svg-copy svg) {
  width: 13px;
  height: 13px;
}

.pilot-md :deep(.pilot-svg-quote:hover),
.pilot-md :deep(.pilot-svg-copy:hover) {
  color: var(--accent);
  background: var(--bg-card);
}

.pilot-md :deep(.pilot-svg-stage) {
  padding: 12px;
  overflow-x: auto;
  display: flex;
  justify-content: center;
  align-items: center;
  background: var(--bg-card);
}

.pilot-md :deep(.pilot-svg-stage svg) {
  display: block;
  width: 100%;
  height: auto;
  max-width: 720px;
}

.pilot-md :deep(.pilot-svg-source) {
  padding: 10px 12px;
  background: var(--bg-card);
  overflow-x: auto;
}

.pilot-md :deep(.pilot-svg-source pre) {
  margin: 0;
  padding: 0;
  background: none;
}

.pilot-md :deep(.pilot-svg-source code) {
  font-size: 12px;
  line-height: 1.5;
  color: var(--text-primary);
}

.pilot-md :deep(.pilot-svg.is-pending) {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 56px;
  padding: 12px;
  color: var(--text-secondary);
  font-size: 12px;
}

.pilot-md :deep(.pilot-svg.is-drawing) {
  border-color: var(--accent-a30);
  box-shadow: 0 0 12px var(--accent-a08);
}

.pilot-md :deep(.pilot-svg-badge.is-drawing) {
  color: var(--accent);
}

.pilot-md :deep(.pilot-svg-badge.is-drawing svg) {
  animation: pilot-svg-spin 2s linear infinite;
}

@keyframes pilot-svg-spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

@media (prefers-reduced-motion: reduce) {
  .pilot-md :deep(.pilot-svg-badge.is-drawing svg) {
    animation: none;
  }
}

.pilot-md :deep(h3) {
  margin: 10px 0 4px;
  padding-left: 8px;
  border-left: 3px solid var(--accent);
  font-size: 13px;
  font-weight: 600;
  line-height: 1.5;
  color: var(--text-primary);
}

.pilot-md :deep(h3:first-child) {
  margin-top: 0;
}

.pilot-md :deep(mark) {
  padding: 0 3px;
  border-radius: 4px;
  background: var(--accent-a15);
  color: var(--text-primary);
  font-weight: 600;
}

.pilot-md :deep(blockquote) {
  margin: 4px 0 8px;
  padding: 8px 10px;
  border-left: 3px solid var(--accent);
  border-radius: 0 8px 8px 0;
  background: var(--accent-a10);
}

.pilot-md :deep(blockquote p) {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
}

.pilot-md :deep(hr) {
  margin: 8px 0;
  border: none;
  border-top: 1px solid var(--border-soft);
}

.pilot-md :deep(.pilot-table-wrap) {
  margin: 8px 0 10px;
  max-width: 100%;
  overflow-x: auto;
  border-radius: 8px;
  border: 1px solid var(--border-soft);
  background: var(--bg-card);
}

.pilot-md :deep(.pilot-table) {
  width: 100%;
  min-width: 100%;
  border-collapse: collapse;
  font-size: 12px;
  line-height: 1.5;
  text-align: left;
}

.pilot-md :deep(.pilot-table th) {
  padding: 8px 10px;
  background: var(--surface-inset);
  color: var(--text-secondary);
  font-weight: 600;
  border-bottom: 1px solid var(--border-soft);
  white-space: nowrap;
}

.pilot-md :deep(.pilot-table td) {
  padding: 7px 10px;
  border-bottom: 1px solid var(--border-soft);
  color: var(--text-primary);
}

.pilot-md :deep(.pilot-table tr:last-child td) {
  border-bottom: none;
}

.pilot-md :deep(.pilot-table tbody tr:hover) {
  background: var(--accent-a10);
}
</style>
