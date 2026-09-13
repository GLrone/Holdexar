<script setup lang="ts">
/**
 * CurrencyFlag —— 货币国旗 + 币种名组件（与 RegionFlag 同为默认框架约束）。
 *
 * 约束：
 * 1. 界面上出现货币的地方，一律展示**币种名**并带所属地区国旗，禁止以代号作为
 *    主显示。（名字随语言：中文「美元」/ 英文 "US Dollar"——约束要的是「名字而
 *    非代号」，不是「恒为中文」。）
 * 2. 货币代号只作次要信息（下拉选项等识别场景自行附加）。
 */
import { computed } from 'vue'
import { currencyFlagUrl, currencyName } from '@/api/currencies'

const props = withDefaults(defineProps<{
  /** 币种代号（USD / JPY …） */
  code: string
  /** 是否显示国旗（默认 true） */
  showFlag?: boolean
}>(), {
  showFlag: true,
})

const name = computed(() => currencyName(props.code))
const flagSrc = computed(() => currencyFlagUrl(props.code))
</script>

<template>
  <span class="currency-flag">
    <img
      v-if="showFlag && flagSrc"
      :src="flagSrc"
      class="currency-flag__icon"
      :alt="code"
      loading="lazy"
      @error="(e: Event) => (e.target as HTMLImageElement).style.display = 'none'"
    />
    <span class="currency-flag__name">{{ name }}</span>
  </span>
</template>

<style scoped>
.currency-flag {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  white-space: nowrap;
}

.currency-flag__icon {
  width: 18px;
  height: 13px;
  border-radius: 2px;
  flex-shrink: 0;
  object-fit: cover;
}

.currency-flag__name {
  font-size: inherit;
  color: inherit;
}
</style>
