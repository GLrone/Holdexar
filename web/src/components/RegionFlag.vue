<script setup lang="ts">
/**
 * RegionFlag —— 全局国旗+地区名组件（写死在默认框架中）。
 *
 * 约束：
 * 1. 所有出现国家或地区**名称**的地方，旁边都要带上国旗图标。
 * 2. 中国香港、中国台湾为固有命名，不得改为"香港特别行政区"等变体。
 *    （中文侧取服务端下发的名字，正是这条政策的落地处，见 stores/regions.ts。）
 * 3. 列表模式等窄列场景可传 compact，超过 4 字的地区名自动简写为"某某区"。
 *    后缀随语言：中文 `区`，英文空串 = 不简写（"Ka区" 不是英文），详见
 *    api/regions.ts 的 compactRegionName。
 */
import { computed } from 'vue'
import { compactRegionName, flagUrl } from '@/api/regions'
import { useI18n } from '@/locales'
import { useRegionsStore } from '@/stores/regions'

const props = withDefaults(defineProps<{
  /** 区服代码（cn / hk / tw / us …） */
  code: string
  /** 紧凑模式：地区名超过 4 字时简写为"XX区" */
  compact?: boolean
  /** 是否显示国旗（默认 true） */
  showFlag?: boolean
  /** 额外 class */
  class?: string
}>(), {
  compact: false,
  showFlag: true,
})

const regionsStore = useRegionsStore()
const { t } = useI18n()

const fullName = computed(() => regionsStore.regionName(props.code))

const displayName = computed(() =>
  props.compact ? compactRegionName(fullName.value, t('common.regionSuffix')) : fullName.value,
)

const flagSrc = computed(() => flagUrl(props.code))
</script>

<template>
  <span class="region-flag" :class="props.class">
    <img
      v-if="showFlag"
      :src="flagSrc"
      class="region-flag__icon"
      :alt="code"
      loading="lazy"
      @error="(e: Event) => (e.target as HTMLImageElement).style.display = 'none'"
    />
    <span class="region-flag__name">{{ displayName }}</span>
  </span>
</template>

<style scoped>
.region-flag {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  white-space: nowrap;
}

.region-flag__icon {
  width: 18px;
  height: 13px;
  border-radius: 2px;
  flex-shrink: 0;
  object-fit: cover;
}

.region-flag__name {
  font-size: inherit;
  color: inherit;
}
</style>
