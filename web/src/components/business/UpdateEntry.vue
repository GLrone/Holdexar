<script setup lang="ts">
/**
 * 顶栏更新提示入口：发现新版本 / 下载中 / 待重启时出现的胶囊。
 *
 * 悬停展开更新内容浮层（HlTooltip 富内容插槽），点击打开更新报告窗口。
 * 下载中保持可点并改显百分比——进度不再只活在弹窗里，弹窗关掉也看得见。
 */
import { computed } from 'vue'

import HlIcon from '@/components/ui/HlIcon.vue'
import HlSpinner from '@/components/ui/HlSpinner.vue'
import HlTooltip from '@/components/ui/HlTooltip.vue'
import { useI18n } from '@/locales'
import { useUpdaterStore } from '@/stores/updater'

import UpdateNotes from './UpdateNotes.vue'

const updater = useUpdaterStore()
const { t } = useI18n()

/** 无事可报时整颗胶囊不出现（发现新版 / 下载中 / 已暂存待重启三种情况才露面） */
const visible = computed(
  () => updater.hasUpdate || Boolean(updater.pendingTag) || updater.downloading,
)

const version = computed(() =>
  (updater.info?.latest || updater.pendingTag || '').replace(/^v/, ''),
)

const percent = computed(() => Math.max(0, Math.min(100, updater.progress?.percent ?? 0)))

const notes = computed(() => updater.info?.notes || '')

/** 发布日期：取 ISO 日期段（清单时间是 UTC，直接截取即是发布当天的稳定口径） */
const releaseDate = computed(() => (updater.info?.publishedAt || '').slice(0, 10))

const notesTitle = computed(() => t('updateDialog.notesTitle', { version: version.value }))
</script>

<template>
  <HlTooltip v-if="visible" rich placement="bottom" align="end" :content="notesTitle">
    <template #popper>
      <div class="upd-entry__pop">
        <div class="upd-entry__pop-head">
          <span class="upd-entry__pop-title">{{ notesTitle }}</span>
          <span v-if="releaseDate" class="upd-entry__pop-date">
            <HlIcon name="calendar" :size="12" />
            {{ t('updateDialog.releaseDate', { date: releaseDate }) }}
          </span>
        </div>
        <div class="upd-entry__pop-body">
          <UpdateNotes v-if="notes" :notes="notes" :version="version" />
          <p v-else class="upd-entry__pop-empty">{{ t('updateDialog.notesEmpty') }}</p>
        </div>
      </div>
    </template>

    <button
      type="button"
      class="upd-entry"
      :class="{ 'is-downloading': updater.downloading }"
      :title="t('updateDialog.entryTip')"
      @click="updater.openDialog()"
    >
      <HlSpinner v-if="updater.downloading" class="upd-entry__spin" />
      <HlIcon v-else name="download" :size="13" />
      <span class="upd-entry__txt">
        {{ updater.downloading ? `${percent}%` : t('updateDialog.entry') }}
      </span>
    </button>
  </HlTooltip>
</template>

<style scoped>
.upd-entry {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 10px;
  border: 1px solid var(--success-a40);
  border-radius: 999px;
  background: var(--success-a15);
  color: var(--success);
  font-size: 12px;
  font-weight: 600;
  line-height: 1.4;
  cursor: pointer;
  transition:
    background calc(var(--duration-1) * var(--motion-scale)) var(--ease-inout),
    border-color calc(var(--duration-1) * var(--motion-scale)) var(--ease-inout);
}

.upd-entry:hover {
  background: var(--success-a30);
  border-color: var(--success-a60);
}

/* 下载中改中性底：它此时表达的是「在跑」而不是「有好消息」 */
.upd-entry.is-downloading {
  background: var(--accent-a10);
  border-color: var(--accent-a40);
  color: var(--accent);
}

.upd-entry.is-downloading:hover {
  background: var(--accent-a20);
}

.upd-entry :deep(.hl-spinner) {
  width: 12px;
  height: 12px;
  border-width: 2px;
}

.upd-entry__txt {
  font-variant-numeric: tabular-nums;
}

.upd-entry__pop {
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 22rem;
  max-width: calc(100vw - 2rem);
  text-align: left;
}

.upd-entry__pop-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  min-width: 0;
}

.upd-entry__pop-title {
  min-width: 0;
  overflow: hidden;
  font-size: 12.5px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-primary);
}

.upd-entry__pop-date {
  display: inline-flex;
  flex-shrink: 0;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  color: var(--text-muted);
}

.upd-entry__pop-body {
  max-height: 18rem;
  overflow-y: auto;
  padding-top: 8px;
  border-top: 1px solid var(--border-soft);
}

.upd-entry__pop-empty {
  margin: 0;
  font-size: 12px;
  color: var(--text-muted);
}
</style>