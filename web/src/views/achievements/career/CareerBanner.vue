<script setup lang="ts">
/* 生涯板块的抬头：模块名 + 口径说明 + 生涯起点/跨度 + 「重新推导」。
 *
 * 它是**唯一**跨分节的生涯元件（不属于任何 data-section）：生涯八块与原
 * 成就四节交错落位，抬头若绑在某一节里会跟着乱跑。放在页面最顶、八块之前，
 * 视觉上就是"下面这一大片都是生涯"。
 */
import { computed } from 'vue'

import { HlButton, HlEmpty, HlIcon, message } from '@/components/ui'
import { useI18n } from '@/locales'
import { useCareerStore } from '@/stores/career'

const { t } = useI18n()
const store = useCareerStore()

const career = computed(() => store.career)

async function refresh(): Promise<void> {
  await store.load(true)
  if (!store.failed) message.success(t('achievements.career.panel.refreshed'))
}
</script>

<template>
  <div class="cr-banner card">
    <div class="cr-banner-main">
      <span class="cr-banner-title">
        <HlIcon name="layers" />
        {{ t('achievements.career.panel.title') }}
      </span>
      <span class="cr-banner-hint">{{ t('achievements.career.panel.hint') }}</span>
      <span v-if="career" class="cr-banner-meta">
        <span v-if="career.activity.firstDate">
          {{ t('achievements.career.panel.firstUnlock', { date: career.activity.firstDate }) }}
        </span>
        <span class="hl-num">
          {{ t('achievements.career.panel.span', { n: career.activity.spanDays }) }}
        </span>
      </span>
    </div>
    <HlButton art="outline" tone="blue" size="sm" :loading="store.refreshing" @click="refresh">
      {{ t('achievements.career.panel.refresh') }}
    </HlButton>
  </div>

  <div v-if="store.failed" class="cr-banner card">
    <HlEmpty size="sm" icon="" :text="t('achievements.career.panel.failed')" />
    <HlButton art="outline" tone="blue" size="sm" @click="store.load()">
      {{ t('achievements.career.panel.retry') }}
    </HlButton>
  </div>
</template>

<style scoped>
.cr-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.cr-banner-main {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}

.cr-banner-title {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 14px;
  font-weight: 700;
  color: var(--text-primary);
}

.cr-banner-hint {
  font-size: 11px;
  color: var(--text-secondary);
}

.cr-banner-meta {
  display: flex;
  gap: 10px;
  font-size: 10.5px;
  color: var(--text-dim);
}
</style>