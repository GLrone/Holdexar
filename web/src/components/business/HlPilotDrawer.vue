<script setup lang="ts">
import { computed } from 'vue'

import { useI18n } from '@/locales'
import HlDrawer from '@/components/ui/HlDrawer.vue'
import HlButton from '@/components/ui/HlButton.vue'
import HlPilotConsole from '@/components/business/HlPilotConsole.vue'

/**
 * 领航台抽屉：顶栏 / 找游戏 / 游戏详情三处入口共开的对话表面。
 * 对话核心全部在 HlPilotConsole（与整页 /pilot 共用同一实现），
 * 本组件只负责抽屉壳（宽度 / 定位 / 关闭动作）。
 */
const props = defineProps<{
  modelValue: boolean
  game?: { appid: number; name: string } | null
}>()
const emit = defineEmits<{ (e: 'update:modelValue', v: boolean): void }>()
const { t } = useI18n()

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})
</script>

<template>
  <HlDrawer
    v-model="visible"
    :modal="false"
    :with-header="false"
    width="420px"
    :top="56"
    class="pilot-drawer"
  >
    <HlPilotConsole :game="game">
      <template #actions>
        <HlButton
          variant="text"
          class="pilot-drawer__close"
          :title="t('common.close')"
          :aria-label="t('common.close')"
          @click="visible = false"
        >
          <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2" stroke-linecap="round" />
          </svg>
        </HlButton>
      </template>
    </HlPilotConsole>
  </HlDrawer>
</template>

<style scoped>
.pilot-drawer__close svg {
  width: 16px;
  height: 16px;
  display: block;
}
</style>
