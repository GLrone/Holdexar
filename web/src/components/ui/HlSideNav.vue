<script setup lang="ts">
import { useRoute, useRouter } from 'vue-router'
import HlIcon from './HlIcon.vue'
import type { IconName } from './icons'
import { useI18n } from '@/locales'

export interface HlSideNavItem {
  label: string
  /** 路由路径；激活判定 = route.path.startsWith(to) */
  to: string
  /** ui/icons.ts 认可清单图标名 */
  icon?: IconName
  /** 产品导览锚点（ProductTour 聚光定位用，如 "sb-proxies"）；缺省按 to 派生 */
  tour?: string
  /** 条目右上角红点（如「我」有新版本可用）；状态提示用，非计数 */
  dot?: boolean
  /** 红点的悬停说明（缺省无 tooltip） */
  dotTitle?: string
}

export interface HlSideNavGroup {
  /** 分组标题（与 component-framework.html 的 .sb-group 一致） */
  label?: string
  items: HlSideNavItem[]
}

withDefaults(
  defineProps<{
    groups: HlSideNavGroup[]
    /** 品牌区 */
    brandName?: string
    brandSubtitle?: string
    /** 品牌 logo 图 URL（浅/深主题由父层切换） */
    logoSrc?: string
    /** 是否显示底部收拢按键 */
    collapsible?: boolean
  }>(),
  { brandName: '', brandSubtitle: '', logoSrc: '', collapsible: true },
)

const collapsed = defineModel<boolean>('collapsed', { default: false })

const emit = defineEmits<{ (e: 'brand-click'): void }>()

const route = useRoute()
const router = useRouter()

function isActive(item: HlSideNavItem) {
  return route.path.startsWith(item.to)
}

function go(item: HlSideNavItem) {
  if (!isActive(item)) router.push(item.to)
}

/** 导览锚点：显式 tour 优先，否则按路由路径派生（/proxies → sb-proxies） */
function tourAttr(item: HlSideNavItem): string | undefined {
  return item.tour ?? (item.to.startsWith('/') ? `sb-${item.to.slice(1)}` : undefined)
}

const { t } = useI18n()

defineOptions({ name: 'HlSideNav' })
</script>

<template>
  <aside class="hl-sidebar" :class="{ 'is-collapsed': collapsed }">
    <!-- 品牌区：logo 悬浮动效，悬停旋转放大；点击派发 brand-click（父层接教程等） -->
    <button type="button" class="hl-sb-brand" :title="t('shell.tour')" @click="emit('brand-click')">
      <div class="hl-sb-brand__logo">
        <img v-if="logoSrc" :src="logoSrc" :alt="brandName" />
        <span v-else style="font-size: 18px; font-weight: 700; color: var(--accent)">
          {{ brandName.charAt(0) || 'H' }}
        </span>
      </div>
      <div class="hl-sb-brand__txt">
        <div class="hl-sb-brand__name">{{ brandName }}</div>
        <div class="hl-sb-brand__sub">{{ brandSubtitle }}</div>
      </div>
    </button>

    <!-- 分组导航：左缘指示条 + 悬停浮起 + 折叠态 tooltip -->
    <nav class="hl-sb-nav">
      <template v-for="(group, gi) in groups" :key="gi">
        <div v-if="group.label" class="hl-sb-group">{{ group.label }}</div>
        <button
          v-for="item in group.items"
          :key="item.to"
          type="button"
          class="hl-sb-item"
          :class="{ 'is-active': isActive(item) }"
          :data-tour="tourAttr(item)"
          @click="go(item)"
        >
          <HlIcon v-if="item.icon" :name="item.icon" :size="17" />
          <span class="hl-sb-item__label">{{ item.label }}</span>
          <span v-if="item.dot" class="hl-sb-dot" :title="item.dotTitle" />
          <span class="hl-sb-tip">{{ item.label }}</span>
        </button>
      </template>
    </nav>

    <!-- 底部：收拢按键 -->
    <div v-if="collapsible" class="hl-sb-foot">
      <button
        type="button"
        class="hl-sb-collapse-btn"
        :title="t(collapsed ? 'shell.sidebar.expand' : 'shell.sidebar.collapse')"
        @click="collapsed = !collapsed"
      >
        <HlIcon :name="collapsed ? 'chevron-right' : 'chevron-left'" :size="16" />
        <span class="hl-sb-collapse-label">{{
          t(collapsed ? 'shell.sidebar.expandShort' : 'shell.sidebar.collapseShort')
        }}</span>
      </button>
    </div>
  </aside>
</template>
