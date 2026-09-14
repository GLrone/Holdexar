<script setup lang="ts">
import { computed } from 'vue'

import HlImg from './HlImg.vue'
import HlTooltip from './HlTooltip.vue'
import { normalizeAvatarUrl } from '@/api/avatar'
import { useI18n, type MessageKey } from '@/locales'

/**
 * 顶栏 Steam 头像（最右上角）—— 对齐 component-framework.html .topbar-avatar 标准。
 * 头像图加载失败回退首字符占位；右下角**Steam 真实在线状态点**（三态）：
 * online（在线=绿）/ ingame（游戏中=蓝，title 带游戏名）/ offline（离线=灰）。
 * 圆形蒙版在 __body 上，状态点露出在蒙版外（外层容器不裁剪）。
 * 悬停气泡 = 框架标准 HlTooltip（wide 自动换行；下方弹出 + 右缘对齐——
 * 宿主贴屏幕顶/右，默认向上居中会出屏）。
 */
type Presence = 'online' | 'ingame' | 'offline'

const props = withDefaults(
  defineProps<{
    src?: string
    name?: string
    /** Steam 真实在线状态（后端 GetPlayerSummaries/miniprofile 双通道） */
    online?: boolean
    /** 正在玩的游戏名（在线且非空 = 游戏中态） */
    inGame?: string
    title?: string
  }>(),
  // `title` 有意留空而非写死中文：withDefaults 的默认值只在 defineProps 求值那刻
  // 算一次，会把语言冻结在组件创建时（HlEmpty 踩过同一个坑）。缺省值在下面的
  // computed 里用 t() 现取，每次渲染都跟着语言走。
  { src: '', name: '', online: false, inGame: '', title: '' },
)

const emit = defineEmits<{ click: [] }>()

const { t } = useI18n()

const resolvedSrc = computed(() => normalizeAvatarUrl(props.src))
const initial = computed(() => props.name.trim().charAt(0).toUpperCase() || 'S')

const presence = computed<Presence>(() =>
  props.online ? (props.inGame ? 'ingame' : 'online') : 'offline',
)

const PRESENCE_KEY: Record<Presence, MessageKey> = {
  online: 'avatar.presenceOnline',
  ingame: 'avatar.presenceInGame',
  offline: 'avatar.presenceOffline',
}

/* 游戏名后缀留在 t() 之外：它是从 Steam 拿到的**数据**，不参与翻译，
   且要整体跟在「状态」之后（放进插值会因中英语序不同而错位）。 */
const statusTitle = computed(() => {
  const game = presence.value === 'ingame' && props.inGame ? ` · ${props.inGame}` : ''
  return (
    t('avatar.status', {
      title: props.title || t('avatar.defaultTitle'),
      status: t(PRESENCE_KEY[presence.value]),
    }) + game
  )
})
</script>

<template>
  <HlTooltip :content="statusTitle" wide placement="bottom" align="end">
    <div
      class="hl-topbar-avatar"
      @click="emit('click')"
    >
      <div class="hl-topbar-avatar__body">
        <!-- 失败自动换 hlretry query 重试（assetCache），全败落首字符 -->
        <HlImg v-if="resolvedSrc" :src="resolvedSrc" :alt="name || 'avatar'">
          <template #fallback>{{ initial }}</template>
        </HlImg>
        <template v-else>{{ initial }}</template>
      </div>
      <span class="hl-topbar-avatar__status" :class="`hl-topbar-avatar__status--${presence}`" />
    </div>
  </HlTooltip>
</template>
