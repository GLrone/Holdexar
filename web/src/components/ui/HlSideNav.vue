<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'

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
    /** 品牌 logo 图 URL（浅/深主题由父层切换） */
    logoSrc?: string
  }>(),
  { brandName: '', logoSrc: '' },
)

const collapsed = defineModel<boolean>('collapsed', { default: false })

/** 品牌区点击：收起态 = 展开（悬停时 logo 已换「侧边栏」图标）。
 *  展开态无动作（原「点击 logo 打开新手引导」已迁至「关于」页 logo）。
 *  收起/展开只保留品牌区这一处入口——旧底部收拢钮已移除：导航项一多它会
 *  被挤出视口，等于没有。
 */
function onBrandClick() {
  if (collapsed.value) collapsed.value = false
}

/* ── 滚动条：默认隐藏，滚动时显形、停手 1.2s 隐去 ──
   宽度由 CSS 的 .is-scrolling 控制（两侧三角形箭头一律不渲染）。 */
const navScrolling = ref(false)
let scrollbarTimer: number | undefined

function onNavScroll() {
  navScrolling.value = true
  if (scrollbarTimer !== undefined) window.clearTimeout(scrollbarTimer)
  scrollbarTimer = window.setTimeout(() => (navScrolling.value = false), 1200)
}

onBeforeUnmount(() => {
  if (scrollbarTimer !== undefined) window.clearTimeout(scrollbarTimer)
})

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
    <!-- 头部：品牌（收起态悬停换「侧边栏」图标、点击展开）+ 展开态收起按键 -->
    <div class="hl-sb-head">
      <button
        type="button"
        class="hl-sb-brand"
        :title="collapsed ? t('shell.sidebar.expand') : ''"
        @click="onBrandClick"
      >
        <div class="hl-sb-brand__logo">
          <img v-if="logoSrc" class="hl-sb-brand__logo-img" :src="logoSrc" :alt="brandName" />
          <span
            v-else
            class="hl-sb-brand__logo-fallback"
            style="font-size: 18px; font-weight: 700; color: var(--accent)"
          >
            {{ brandName.charAt(0) || 'H' }}
          </span>
          <HlIcon v-if="collapsed" name="sidebar" :size="18" class="hl-sb-brand__expand" />
        </div>
        <!-- 副标题（STEAM 多区价格监控终端）已移除：腾出的位置给品牌名与收起键 -->
        <div class="hl-sb-brand__txt">
          <div class="hl-sb-brand__name">{{ brandName }}</div>
        </div>
      </button>
      <button
        v-if="!collapsed"
        type="button"
        class="hl-sb-toggle"
        :title="t('shell.sidebar.collapse')"
        @click="collapsed = true"
      >
        <HlIcon name="sidebar" :size="16" />
      </button>
    </div>

    <!-- 分组导航：左缘指示条 + 悬停浮起 + 折叠态原生 title -->
    <nav
      class="hl-sb-nav"
      :class="{ 'is-scrolling': navScrolling }"
      @scroll="onNavScroll"
      @wheel="onNavScroll"
    >
      <template v-for="(group, gi) in groups" :key="gi">
        <div v-if="group.label" class="hl-sb-group">{{ group.label }}</div>
        <button
          v-for="item in group.items"
          :key="item.to"
          type="button"
          class="hl-sb-item"
          :class="{ 'is-active': isActive(item) }"
          :data-tour="tourAttr(item)"
          :title="collapsed ? item.label : undefined"
          @click="go(item)"
        >
          <HlIcon v-if="item.icon" :name="item.icon" :size="17" />
          <span class="hl-sb-item__label">{{ item.label }}</span>
          <span v-if="item.dot" class="hl-sb-dot" :title="item.dotTitle" />
        </button>
      </template>
    </nav>

  </aside>
</template>
