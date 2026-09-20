<script setup lang="ts">
/* TrophyMedal —— 奖杯陈列件（assets/achievements/ 四档）。
 *
 * 正向渲染 = 奖杯立绘 PNG（高度 = size，宽度按素材原始比例自适应）；
 * 素材加载失败回退到内嵌 SVG 勋章（同一 tier 语义，不空窗）。
 * glow：压在深色封面上的外发光（颜色随档位）。
 * tier：platinum（白金殿堂）/ gold（传说）/ silver / bronze。
 *
 * ⚠️ 尺寸必须走 **CSS**（内联 style），不能写 `height="24"` 属性：
 * 全局引入了 Tailwind（`styles/tokens.css` 的 `@import 'tailwindcss'`），
 * 其 preflight 有一条 author 级规则 `img, video { height: auto }`。而 HTML 的
 * height/width 属性只是**呈现提示**（presentational hint），层叠优先级低于任何
 * author 规则——于是属性永远被压成 auto，`<img>` 一律按素材固有尺寸（这四张都
 * 是 320 高一档）铺开，四处陈列点全部炸成 320px 高。
 * SVG 分支不受影响（preflight 只针对 img/video）。
 */
import { computed, ref, watch } from 'vue'

/* 立绘走 import 引入（Vite 内容指纹）：换图即换 URL，浏览器 immutable 缓存
   不会把旧图多留一天——public/ 下无指纹素材要按天过期，不适合高频替换。 */
import trophyPlatinum from '@/assets/achievements/trophy_platinum.png'
import trophyGold from '@/assets/achievements/trophy_gold.png'
import trophySilver from '@/assets/achievements/trophy_silver.png'
import trophyBronze from '@/assets/achievements/trophy_bronze.png'

type MedalTier = 'platinum' | 'gold' | 'silver' | 'bronze'

const props = withDefaults(
  defineProps<{
    tier?: MedalTier
    /** 显示高度 px（宽度按素材比例自适应） */
    size?: number
    /** 外发光（压在深色封面上的奖杯用，从图里跳出来） */
    glow?: boolean
    /** 环境动效：轻浮动 + 光晕呼吸（模块标题这类陈列位专用，默认关） */
    animated?: boolean
  }>(),
  { tier: 'gold', size: 44, glow: false, animated: false },
)

const ASSETS: Record<MedalTier, string> = {
  platinum: trophyPlatinum,
  gold: trophyGold,
  silver: trophySilver,
  bronze: trophyBronze,
}

/* 四档素材的固有像素尺寸（四张高度同为 320，宽度各异）。
 * 用途有二：① 按高度反推宽度，四种档位在任意 size 下都保持各自比例；
 * ② 加载前就有确定盒宽，避免「先 0 宽后撑开」导致文字位移。 */
const ASSET_BOX: Record<MedalTier, readonly [number, number]> = {
  platinum: [181, 320],
  silver: [173, 320],
  gold: [155, 320],
  bronze: [148, 320],
}

/* SVG 兜底的档位色（tokens.css --medal-*，主题无关金属定色） */
const TIER_VARS: Record<MedalTier, { c1: string; c2: string; c3: string; ring: string; glyph: string; glowColor: string }> = {
  platinum: {
    c1: 'var(--medal-platinum-1)', c2: 'var(--medal-platinum-2)', c3: 'var(--medal-platinum-3)',
    ring: 'var(--medal-platinum-ring)', glyph: 'var(--medal-platinum-glyph)', glowColor: 'var(--medal-platinum-ring)',
  },
  gold: {
    c1: 'var(--medal-gold-1)', c2: 'var(--medal-gold-2)', c3: 'var(--medal-gold-3)',
    ring: 'var(--medal-gold-ring)', glyph: 'var(--medal-gold-glyph)', glowColor: 'var(--medal-gold-2)',
  },
  silver: {
    c1: 'var(--medal-silver-1)', c2: 'var(--medal-silver-2)', c3: 'var(--medal-silver-3)',
    ring: 'var(--medal-silver-ring)', glyph: 'var(--medal-silver-glyph)', glowColor: 'var(--medal-silver-ring)',
  },
  bronze: {
    c1: 'var(--medal-bronze-1)', c2: 'var(--medal-bronze-2)', c3: 'var(--medal-bronze-3)',
    ring: 'var(--medal-bronze-ring)', glyph: 'var(--medal-bronze-glyph)', glowColor: 'var(--medal-bronze-2)',
  },
}

const vars = computed(() => TIER_VARS[props.tier])
const gid = computed(() => `tm-${props.tier}`)
const failed = ref(false)
watch(() => props.tier, () => { failed.value = false })

/** 两种状态（立绘 / SVG 兜底）共用同一盒子：切换时布局零位移 */
const figureStyle = computed<Record<string, string>>(() => {
  const [w, h] = ASSET_BOX[props.tier]
  return {
    height: `${props.size}px`,
    width: `${Math.round((props.size * w) / h)}px`,
    ...(props.glow ? { '--medal-glow': vars.value.glowColor } : {}),
  }
})

/* 奖杯徽记路径（杯身 / 左耳 / 右耳）——兜底 SVG 的浮雕层与填充层共用 */
const GLYPH_PATHS = [
  'M23.2 16.8h17.6v9.4c0 5.7-3.9 9.9-8.8 9.9s-8.8-4.2-8.8-9.9z',
  'M20.2 19.5h-3.4c-1.7 0-2.6 1.2-2.6 2.9 0 3.5 2.3 6.4 6 6.4h1.2v-3.1h-.9c-1.7 0-2.6-1.4-2.6-3.1 0-.3.2-.5.5-.5h1.8v-2.6z',
  'M43.8 19.5h3.4c1.7 0 2.6 1.2 2.6 2.9 0 3.5-2.3 6.4-6 6.4h-1.2v-3.1h.9c1.7 0 2.6-1.4 2.6-3.1 0-.3-.2-.5-.5-.5h-1.8v-2.6z',
]
</script>

<template>
  <!-- 正向：奖杯立绘（尺寸走 style，见文件头 preflight 说明） -->
  <img
    v-if="!failed"
    class="trophy-figure"
    :class="{ 'is-glow': glow, 'is-animated': animated }"
    :style="figureStyle"
    :src="ASSETS[tier]"
    alt=""
    draggable="false"
    @error="failed = true"
  />
  <!-- 兜底：SVG 金属勋章（素材缺失/加载失败时不空窗）。
       同盒不同形：勋章是 64x64 圆盘，缩进窄高盒里居中——盒子尺寸与立绘一致，
       故两种状态切换不会让周边文字跳位。 -->
  <svg
    v-else
    class="trophy-figure is-svg"
    :class="{ 'is-glow': glow, 'is-animated': animated }"
    :style="figureStyle"
    viewBox="0 0 64 64"
    preserveAspectRatio="xMidYMid meet"
    role="img"
    aria-hidden="true"
  >
    <defs>
      <radialGradient :id="gid + '-face'" cx="0.36" cy="0.3" r="0.86">
        <stop offset="0" :stop-color="vars.c1" />
        <stop offset="0.42" :stop-color="vars.c2" />
        <stop offset="0.84" :stop-color="vars.c3" />
        <stop offset="1" :stop-color="vars.c3" />
      </radialGradient>
      <linearGradient :id="gid + '-rim'" x1="0.2" y1="1" x2="0.8" y2="0">
        <stop offset="0" :stop-color="vars.c1" />
        <stop offset="0.45" :stop-color="vars.ring" />
        <stop offset="1" :stop-color="vars.c3" />
      </linearGradient>
      <linearGradient :id="gid + '-glyph'" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" :stop-color="vars.glyph" stop-opacity="0.45" />
        <stop offset="0.38" :stop-color="vars.glyph" />
        <stop offset="1" :stop-color="vars.glyph" stop-opacity="0.95" />
      </linearGradient>
      <clipPath :id="gid + '-disc'">
        <circle cx="32" cy="32" r="30" />
      </clipPath>
    </defs>
    <circle cx="32" cy="32" r="30" :fill="`url(#${gid}-face)`" />
    <circle cx="32" cy="32" r="29" fill="none" :stroke="`url(#${gid}-rim)`" stroke-width="2.2" />
    <circle cx="32" cy="32" r="24.6" fill="none" :stroke="vars.c3" stroke-opacity="0.55" stroke-width="1.1" />
    <circle cx="32" cy="32" r="22.4" fill="none" :stroke="vars.ring" stroke-opacity="0.26" stroke-width="0.7" />
    <g :fill="vars.ring" opacity="0.55" transform="translate(0 0.6)">
      <path v-for="(d, i) in GLYPH_PATHS" :key="`e${i}`" :d="d" />
      <rect x="30.4" y="35.6" width="3.2" height="5.2" rx="1" />
      <path d="M26.2 40.6h11.6l1.5 4.6a1.2 1.2 0 0 1-1.1 1.6H25.8a1.2 1.2 0 0 1-1.1-1.6z" />
    </g>
    <g :fill="`url(#${gid}-glyph)`">
      <path v-for="(d, i) in GLYPH_PATHS" :key="`g${i}`" :d="d" />
      <rect x="30.4" y="35.6" width="3.2" height="5.2" rx="1" />
      <path d="M26.2 40.6h11.6l1.5 4.6a1.2 1.2 0 0 1-1.1 1.6H25.8a1.2 1.2 0 0 1-1.1-1.6z" />
    </g>
    <circle cx="32" cy="24.4" r="3.1" :fill="vars.ring" :stroke="vars.glyph" stroke-opacity="0.45" stroke-width="0.7" />
    <circle cx="30.9" cy="23.3" r="0.95" :fill="vars.c1" fill-opacity="0.9" />
  </svg>
</template>

<style scoped>
.trophy-figure {
  display: inline-block;
  vertical-align: middle;
  flex-shrink: 0;
  /* 尺寸由调用方按 size + 素材比例写进内联 style；这里只兜住盒模型与不拉伸 */
  object-fit: contain;
  user-select: none;
}
/* 外发光：压在深色封面上的奖杯要能从图里跳出来 */
.is-glow {
  filter: drop-shadow(0 1px 3px rgba(0, 0, 0, 0.45))
    drop-shadow(0 0 7px color-mix(in srgb, var(--medal-glow) 50%, transparent));
}
/* 陈列位环境动效：轻浮动（transform）+ 光晕呼吸（filter）双轨并行。
   仅标题这类单枚陈列位开启；循环动画在 reduced-motion 下显式关停。 */
.is-animated {
  animation: tmFloat 3.6s ease-in-out var(--tm-float-delay, 0s) infinite;
}
.is-animated.is-glow {
  animation: tmFloat 3.6s ease-in-out var(--tm-float-delay, 0s) infinite,
    tmGlowBreath 3.6s ease-in-out var(--tm-float-delay, 0s) infinite;
}
@keyframes tmFloat {
  0%, 100% { transform: translateY(0); }
  50% { transform: translateY(-2.5px); }
}
@keyframes tmGlowBreath {
  0%, 100% {
    filter: drop-shadow(0 1px 3px rgba(0, 0, 0, 0.45))
      drop-shadow(0 0 4px color-mix(in srgb, var(--medal-glow) 28%, transparent));
  }
  50% {
    filter: drop-shadow(0 1px 3px rgba(0, 0, 0, 0.45))
      drop-shadow(0 0 10px color-mix(in srgb, var(--medal-glow) 80%, transparent));
  }
}
@media (prefers-reduced-motion: reduce) {
  .is-animated,
  .is-animated.is-glow { animation: none; }
}
</style>