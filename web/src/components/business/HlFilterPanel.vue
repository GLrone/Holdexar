<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useFilterStore } from '@/stores/gamesFilter'
import { flagUrl, type RegionMeta } from '@/api/regions'
import { useRegionsStore } from '@/stores/regions'
import { HlCheckbox, HlDrawer } from '@/components/ui'
import { useI18n } from '@/locales'

/**
 * 高级筛选 —— component-framework.html 标准结构（hl-fp-*）+ 右侧抽屉壳（HlDrawer）。
 * 分区标题 + HlCheckbox（史低蓝/绿变体）+ 令牌化输入 + 地区/差价弹层（幕布选中态）。
 * ownership 维度已接入「隐藏已拥有」（服务端过滤）；系列维度无数据源，暂未渲染。
 */
const store = useFilterStore()
const regionsStore = useRegionsStore()
const { t } = useI18n()

const showRegionPopup = ref(false)
const showDiffTypeMenu = ref(false)
const regionSelectorRef = ref<HTMLElement | null>(null)
const diffTypeRef = ref<HTMLElement | null>(null)

/* 空表兜底（后端不可达 / 启动首帧）：只兜 code 与 currency。
   **区名不在这里写死** —— 区服名以 stores/regions 为单一来源（含它「表外区码
   回落区码本身」的既定口径），组件侧只问它要名字。 */
const FALLBACK_REGION = { code: 'CN', currency: 'CNY' } as const

const selectedRegion = computed<RegionMeta>(
  () =>
    regionsStore.metas.find((r) => r.code === store.filterRegion.toUpperCase()) ??
    regionsStore.metas[0] ?? {
      ...FALLBACK_REGION,
      name: regionsStore.regionName(FALLBACK_REGION.code),
    },
)

/* 价格范围地区选项 = 国区 + 启用区（追踪区）。与 HlNavbar 地区下拉同口径：
   GPW/低价数据只按启用区抓取，未启用区列出来必空结果（全量 41 区是误导） */
const priceRegionOptions = computed(() =>
  regionsStore.metas.filter(
    (r) => r.code === 'CN' || regionsStore.enabledCodes.includes(r.code.toLowerCase()),
  ),
)

function currencyLabel() {
  return selectedRegion.value.currency
}

function handleSelectRegion(code: string) {
  store.setFilter('filterRegion', code)
  showRegionPopup.value = false
}

/* 持久化的旧筛选可能指向已停用的区（下拉收敛后不可见）：自动回落国区，
   避免价格区间按一个无数据区筛选出空列表 */
watch(priceRegionOptions, (opts) => {
  if (!opts.some((r) => r.code.toLowerCase() === store.filterRegion)) {
    store.setFilter('filterRegion', 'cn')
  }
})

function handleSelectDiffType(type: 'absolute' | 'percent') {
  store.setFilter('diffType', type)
  showDiffTypeMenu.value = false
}

function onDocClick(e: MouseEvent) {
  if (regionSelectorRef.value && !regionSelectorRef.value.contains(e.target as Node)) {
    showRegionPopup.value = false
  }
  if (diffTypeRef.value && !diffTypeRef.value.contains(e.target as Node)) {
    showDiffTypeMenu.value = false
  }
}

onMounted(() => document.addEventListener('mousedown', onDocClick))
onBeforeUnmount(() => document.removeEventListener('mousedown', onDocClick))
</script>

<template>
  <!-- 右侧弹出抽屉（不再使用浮动窗口） -->
  <HlDrawer
    v-model="store.showAdvancedFilter"
    :title="t('filterPanel.advanced.title')"
    width="400px"
    :mask-closable="true"
  >
    <div class="hl-fp-body" style="padding: 0">
      <!-- 基础过滤 -->
      <div>
        <div class="hl-fp-sec-title">{{ t('filterPanel.section.basic') }}</div>
        <div class="hl-fp-check">
          <HlCheckbox
            :model-value="store.top3Check"
            @update:model-value="(v: boolean) => store.setFilter('top3Check', v)"
          >
            {{ t('filterPanel.basic.top3') }}
            <span
              v-if="store.layoutMode === 'list'"
              style="font-size: 11px; color: var(--text-muted); margin-left: 6px"
            >
              {{ t('filterPanel.basic.top3ListHint') }}
            </span>
          </HlCheckbox>
        </div>
        <div class="hl-fp-inline">
          <HlCheckbox
            :model-value="store.strictLowest"
            :label="t('filterPanel.basic.strictLowest')"
            @update:model-value="(v: boolean) => store.setFilter('strictLowest', v)"
          />
          <span class="hl-fp-hint">{{ t('filterPanel.basic.tolerance') }}</span>
          <input
            type="number"
            class="hl-fp-tol"
            :value="store.tolerance"
            min="0"
            step="1"
            @input="store.setFilter('tolerance', ($event.target as HTMLInputElement).value)"
            @keydown.enter.prevent="store.setFilter('strictLowest', true)"
          />
          <span>{{ t('filterPanel.basic.toleranceUnit') }}</span>
          <span class="hl-fp-hint">{{ t('filterPanel.basic.toleranceHint') }}</span>
        </div>
      </div>

      <!-- 史低状态（框架标准：新史低蓝勾 / 平史低绿勾） -->
      <div>
        <div class="hl-fp-sec-title">{{ t('filterPanel.section.lowest') }}</div>
        <div class="hl-fp-check">
          <HlCheckbox
            variant="hlnew"
            :model-value="store.hlNew"
            :label="t('filterPanel.lowest.new')"
            @update:model-value="(v: boolean) => store.setFilter('hlNew', v)"
          />
          <HlCheckbox
            variant="hleq"
            :model-value="store.hlEqual"
            :label="t('filterPanel.lowest.equal')"
            @update:model-value="(v: boolean) => store.setFilter('hlEqual', v)"
          />
          <HlCheckbox
            :model-value="store.hlNon"
            :label="t('filterPanel.lowest.non')"
            @update:model-value="(v: boolean) => store.setFilter('hlNon', v)"
          />
        </div>
      </div>

      <!-- 平台收录 -->
      <div>
        <div class="hl-fp-sec-title">{{ t('filterPanel.section.platform') }}</div>
        <div class="hl-fp-check">
          <HlCheckbox
            :model-value="store.onlyEpic"
            :label="t('filterPanel.platform.epic')"
            @update:model-value="(v: boolean) => store.setFilter('onlyEpic', v)"
          />
          <HlCheckbox
            :model-value="store.onlyHb"
            :label="t('filterPanel.platform.hb')"
            @update:model-value="(v: boolean) => store.setFilter('onlyHb', v)"
          />
          <HlCheckbox
            :model-value="store.onlyXgp"
            :label="t('filterPanel.platform.xgp')"
            @update:model-value="(v: boolean) => store.setFilter('onlyXgp', v)"
          />
        </div>
      </div>

      <!-- 价格范围（指定地区） -->
      <div style="position: relative">
        <div class="hl-fp-sec-title">{{ t('filterPanel.section.priceRange') }}</div>
        <div
          ref="regionSelectorRef"
          class="hl-fp-region-sel"
          @click="showRegionPopup = !showRegionPopup"
        >
          <span class="hl-rf hl-rf--sm">
            <img :src="flagUrl(selectedRegion.code)" :alt="selectedRegion.code" />
            {{ selectedRegion.name }}
          </span>
          <span class="car">▼</span>
        </div>
        <div class="hl-fp-region-pop" :class="{ show: showRegionPopup }">
          <div
            v-for="r in priceRegionOptions"
            :key="r.code"
            class="hl-fp-region-pop__item"
            :class="{ 'is-active': store.filterRegion === r.code.toLowerCase() }"
            @click="handleSelectRegion(r.code.toLowerCase())"
          >
            <span class="hl-rf hl-rf--sm">
              <img :src="flagUrl(r.code)" :alt="r.code" />
              {{ r.name }}
            </span>
          </div>
        </div>
        <div class="hl-fp-range-row" style="margin-top: 10px">
          <div class="hl-input-wrap hl-input-wrap--sm">
            <input
              type="number"
              placeholder="0"
              :value="store.minPrice"
              @input="store.setFilter('minPrice', ($event.target as HTMLInputElement).value)"
            />
          </div>
          <span style="color: var(--text-dim)">—</span>
          <div class="hl-input-wrap hl-input-wrap--sm">
            <input
              type="number"
              placeholder="∞"
              :value="store.maxPrice"
              @input="store.setFilter('maxPrice', ($event.target as HTMLInputElement).value)"
            />
          </div>
          <span class="hl-fp-cur">{{ currencyLabel() }}</span>
        </div>
      </div>

      <!-- 与国区差价 -->
      <div>
        <div class="hl-fp-sec-title hl-fp-sec-flex">
          <span>{{ t('filterPanel.section.diff') }}</span>
          <div style="display: flex; align-items: center; gap: 8px">
            <HlCheckbox
              style="font-size: 11px"
              :model-value="store.giftFilter"
              :label="t('filterPanel.diff.giftOnly')"
              @update:model-value="(v: boolean) => store.setFilter('giftFilter', v)"
            />
            <div ref="diffTypeRef" class="hl-fp-diff">
              <div class="hl-fp-diff-sel" @click="showDiffTypeMenu = !showDiffTypeMenu">
                <span>{{
                  store.diffType === 'absolute'
                    ? t('filterPanel.diff.absolute')
                    : t('filterPanel.diff.percent')
                }}</span>
                <span style="font-size: 9px; color: var(--text-muted)">▼</span>
              </div>
              <div class="hl-fp-diff-pop" :class="{ show: showDiffTypeMenu }">
                <div
                  class="hl-fp-diff-pop__item"
                  :class="{ 'is-active': store.diffType === 'absolute' }"
                  @click="handleSelectDiffType('absolute')"
                >
                  {{ t('filterPanel.diff.absolute') }}
                </div>
                <div
                  class="hl-fp-diff-pop__item"
                  :class="{ 'is-active': store.diffType === 'percent' }"
                  @click="handleSelectDiffType('percent')"
                >
                  {{ t('filterPanel.diff.percent') }}
                </div>
              </div>
            </div>
          </div>
        </div>
        <div class="hl-fp-range-row">
          <div class="hl-input-wrap hl-input-wrap--sm">
            <input
              type="number"
              :placeholder="t('filterPanel.diff.minPlaceholder')"
              step="0.01"
              :value="store.diffMin"
              @input="store.setFilter('diffMin', ($event.target as HTMLInputElement).value)"
            />
          </div>
          <span style="color: var(--text-dim)">—</span>
          <div class="hl-input-wrap hl-input-wrap--sm">
            <input
              type="number"
              :placeholder="t('filterPanel.diff.maxPlaceholder')"
              step="0.01"
              :value="store.diffMax"
              @input="store.setFilter('diffMax', ($event.target as HTMLInputElement).value)"
            />
          </div>
          <span class="hl-fp-cur">{{ currencyLabel() }}</span>
        </div>
      </div>

      <!-- 评测量范围 -->
      <div>
        <div class="hl-fp-sec-title">{{ t('filterPanel.section.reviews') }}</div>
        <div class="hl-fp-range-row">
          <div class="hl-input-wrap hl-input-wrap--sm">
            <input
              type="number"
              placeholder="0"
              :value="store.minReviews"
              @input="store.setFilter('minReviews', ($event.target as HTMLInputElement).value)"
            />
          </div>
          <span style="color: var(--text-dim)">—</span>
          <div class="hl-input-wrap hl-input-wrap--sm">
            <input
              type="number"
              placeholder="∞"
              :value="store.maxReviews"
              @input="store.setFilter('maxReviews', ($event.target as HTMLInputElement).value)"
            />
          </div>
          <span class="hl-fp-cur">{{ t('filterPanel.reviews.unit') }}</span>
        </div>
      </div>

      <!-- 好评率范围 -->
      <div>
        <div class="hl-fp-sec-title">{{ t('filterPanel.section.rating') }}</div>
        <div class="hl-fp-range-row">
          <div class="hl-input-wrap hl-input-wrap--sm">
            <input
              type="number"
              placeholder="0"
              min="0"
              max="100"
              :value="store.minRating"
              @input="store.setFilter('minRating', ($event.target as HTMLInputElement).value)"
            />
          </div>
          <span style="color: var(--text-dim)">—</span>
          <div class="hl-input-wrap hl-input-wrap--sm">
            <input
              type="number"
              placeholder="100"
              min="0"
              max="100"
              :value="store.maxRating"
              @input="store.setFilter('maxRating', ($event.target as HTMLInputElement).value)"
            />
          </div>
          <span class="hl-fp-cur">%</span>
        </div>
      </div>

      <!-- 其他 -->
      <div>
        <div class="hl-fp-sec-title">{{ t('filterPanel.section.other') }}</div>
        <div class="hl-fp-check">
          <HlCheckbox
            :model-value="store.onlyDiscounted"
            :label="t('filterPanel.other.onlyDiscounted')"
            @update:model-value="(v: boolean) => store.setFilter('onlyDiscounted', v)"
          />
          <HlCheckbox
            :model-value="store.hideOwned"
            :label="t('filterPanel.other.hideOwned')"
            @update:model-value="(v: boolean) => store.setFilter('hideOwned', v)"
          />
          <HlCheckbox
            :model-value="store.excludeDlc"
            :label="t('filterPanel.other.hideDlc')"
            @update:model-value="(v: boolean) => store.setFilter('excludeDlc', v)"
          />
        </div>
      </div>
    </div>

    <template #footer>
      <button class="filter-btn-reset" @click="store.resetFilters()">
        {{ t('filterPanel.action.reset') }}
      </button>
      <button class="filter-btn-apply" @click="store.showAdvancedFilter = false">
        {{ t('filterPanel.action.apply') }}
      </button>
    </template>
  </HlDrawer>
</template>
