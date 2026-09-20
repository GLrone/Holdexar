<script setup lang="ts">
/* BorderGlow —— 鼠标贴近卡片边缘时沿光标方向浮现的描边辉光。
 * 根节点即卡片表面：背景取主题 token（--bg-card），描边/圆角/阴影也走令牌，
 * 仅辉光与网格渐变边框为刻意设计色（不随主题变）。`light` 控制浅色表面下的
 * 辉光叠加模式。 */
import { ref, computed, onMounted } from 'vue'

interface Props {
  edgeSensitivity?: number
  glowColor?: string
  backgroundColor?: string
  borderRadius?: number
  glowRadius?: number
  glowIntensity?: number
  coneSpread?: number
  animated?: boolean
  colors?: string[]
  fillOpacity?: number
  light?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  edgeSensitivity: 30,
  glowColor: '40 80 80',
  backgroundColor: 'var(--bg-card)',
  borderRadius: 14,
  glowRadius: 40,
  glowIntensity: 1,
  coneSpread: 25,
  animated: false,
  colors: () => ['#c084fc', '#f472b6', '#38bdf8'],
  fillOpacity: 0.5,
  light: false,
})

const cardRef = ref<HTMLElement | null>(null)

function parseHSL(hslStr: string) {
  const match = hslStr.match(/([\d.]+)\s*([\d.]+)%?\s*([\d.]+)%?/)
  if (!match) return { h: 40, s: 80, l: 80 }
  return { h: parseFloat(match[1]), s: parseFloat(match[2]), l: parseFloat(match[3]) }
}

function buildGlowVars(glowColor: string, intensity: number) {
  const { h, s, l } = parseHSL(glowColor)
  const base = `${h}deg ${s}% ${l}%`
  const opacities = [100, 60, 50, 40, 30, 20, 10]
  const keys = ['', '-60', '-50', '-40', '-30', '-20', '-10']
  const vars: Record<string, string> = {}
  for (let i = 0; i < opacities.length; i++) {
    vars[`--glow-color${keys[i]}`] = `hsl(${base} / ${Math.min(opacities[i] * intensity, 100)}%)`
  }
  return vars
}

const GRADIENT_POSITIONS = ['80% 55%', '69% 34%', '8% 6%', '41% 38%', '86% 85%', '82% 18%', '51% 4%']
const GRADIENT_KEYS = [
  '--gradient-one', '--gradient-two', '--gradient-three', '--gradient-four',
  '--gradient-five', '--gradient-six', '--gradient-seven',
]
const COLOR_MAP = [0, 1, 2, 0, 1, 2, 1]

function buildGradientVars(colors: string[]) {
  const vars: Record<string, string> = {}
  for (let i = 0; i < 7; i++) {
    const c = colors[Math.min(COLOR_MAP[i], colors.length - 1)]
    vars[GRADIENT_KEYS[i]] = `radial-gradient(at ${GRADIENT_POSITIONS[i]}, ${c} 0px, transparent 50%)`
  }
  vars['--gradient-base'] = `linear-gradient(${colors[0]} 0 100%)`
  return vars
}

function isLightColor(color: string) {
  const value = color.trim().replace('#', '')
  if (!/^[\da-f]{3}([\da-f]{3})?$/i.test(value)) return false
  const hex = value.length === 3 ? value.split('').map((ch) => ch + ch).join('') : value
  const red = parseInt(hex.slice(0, 2), 16)
  const green = parseInt(hex.slice(2, 4), 16)
  const blue = parseInt(hex.slice(4, 6), 16)
  return red * 0.2126 + green * 0.7152 + blue * 0.0722 > 180
}

function easeOutCubic(x: number) {
  return 1 - Math.pow(1 - x, 3)
}
function easeInCubic(x: number) {
  return x * x * x
}

function animateValue(opts: {
  start?: number
  end?: number
  duration?: number
  delay?: number
  ease?: (x: number) => number
  onUpdate: (v: number) => void
  onEnd?: () => void
}) {
  const { start = 0, end = 100, duration = 1000, delay = 0, ease = easeOutCubic, onUpdate, onEnd } = opts
  const t0 = performance.now() + delay
  function tick() {
    const elapsed = performance.now() - t0
    const t = Math.min(elapsed / duration, 1)
    onUpdate(start + (end - start) * ease(t))
    if (t < 1) requestAnimationFrame(tick)
    else if (onEnd) onEnd()
  }
  setTimeout(() => requestAnimationFrame(tick), delay)
}

const lightSurface = computed(() => props.light || isLightColor(props.backgroundColor))

function getCenterOfElement(el: HTMLElement): [number, number] {
  const { width, height } = el.getBoundingClientRect()
  return [width / 2, height / 2]
}

function getEdgeProximity(el: HTMLElement, x: number, y: number) {
  const [cx, cy] = getCenterOfElement(el)
  const dx = x - cx
  const dy = y - cy
  let kx = Infinity
  let ky = Infinity
  if (dx !== 0) kx = cx / Math.abs(dx)
  if (dy !== 0) ky = cy / Math.abs(dy)
  return Math.min(Math.max(1 / Math.min(kx, ky), 0), 1)
}

function getCursorAngle(el: HTMLElement, x: number, y: number) {
  const [cx, cy] = getCenterOfElement(el)
  const dx = x - cx
  const dy = y - cy
  if (dx === 0 && dy === 0) return 0
  const radians = Math.atan2(dy, dx)
  let degrees = radians * (180 / Math.PI) + 90
  if (degrees < 0) degrees += 360
  return degrees
}

function handlePointerMove(e: PointerEvent) {
  const card = cardRef.value
  if (!card) return
  const rect = card.getBoundingClientRect()
  const x = e.clientX - rect.left
  const y = e.clientY - rect.top
  const edge = getEdgeProximity(card, x, y)
  const angle = getCursorAngle(card, x, y)
  card.style.setProperty('--edge-proximity', `${(edge * 100).toFixed(3)}`)
  card.style.setProperty('--cursor-angle', `${angle.toFixed(3)}deg`)
}

onMounted(() => {
  if (!props.animated || !cardRef.value) return
  const card = cardRef.value
  const angleStart = 110
  const angleEnd = 465
  card.classList.add('sweep-active')
  card.style.setProperty('--cursor-angle', `${angleStart}deg`)
  animateValue({ duration: 500, onUpdate: (v) => card.style.setProperty('--edge-proximity', String(v)) })
  animateValue({
    ease: easeInCubic,
    duration: 1500,
    end: 50,
    onUpdate: (v) => card.style.setProperty('--cursor-angle', `${(angleEnd - angleStart) * (v / 100) + angleStart}deg`),
  })
  animateValue({
    ease: easeOutCubic,
    delay: 1500,
    duration: 2250,
    start: 50,
    end: 100,
    onUpdate: (v) => card.style.setProperty('--cursor-angle', `${(angleEnd - angleStart) * (v / 100) + angleStart}deg`),
  })
  animateValue({
    ease: easeInCubic,
    delay: 2500,
    duration: 1500,
    start: 100,
    end: 0,
    onUpdate: (v) => card.style.setProperty('--edge-proximity', String(v)),
    onEnd: () => card.classList.remove('sweep-active'),
  })
})

const glowVars = computed(() => buildGlowVars(props.glowColor, props.glowIntensity))
const cardStyle = computed(() => ({
  '--card-bg': props.backgroundColor,
  '--edge-sensitivity': props.edgeSensitivity,
  '--border-radius': `${props.borderRadius}px`,
  '--glow-padding': `${props.glowRadius}px`,
  '--cone-spread': props.coneSpread,
  '--fill-opacity': props.fillOpacity,
  ...glowVars.value,
  ...buildGradientVars(props.colors),
}))
</script>

<template>
  <div
    ref="cardRef"
    class="border-glow-card"
    :class="{ 'border-glow-card--light': lightSurface }"
    :style="cardStyle"
    @pointermove="handlePointerMove"
  >
    <span class="edge-light" />
    <div class="border-glow-inner">
      <slot />
    </div>
  </div>
</template>

<style scoped>
.border-glow-card {
  --edge-proximity: 0;
  --cursor-angle: 45deg;
  --edge-sensitivity: 30;
  --color-sensitivity: calc(var(--edge-sensitivity) + 20);
  --border-radius: var(--radius-lg);
  --glow-padding: 40px;
  --cone-spread: 25;

  position: relative;
  border-radius: var(--border-radius);
  isolation: isolate;
  transform: translate3d(0, 0, 0.01px);
  display: grid;
  border: 1px solid var(--border-soft);
  background: var(--card-bg, transparent);
  overflow: visible;
  box-shadow: var(--shadow-sm);
}

.border-glow-card--light::after,
.border-glow-card--light > .edge-light {
  mix-blend-mode: normal;
}

.border-glow-card::before,
.border-glow-card::after,
.border-glow-card > .edge-light {
  content: '';
  position: absolute;
  inset: 0;
  border-radius: inherit;
  transition: opacity 0.25s ease-out;
  z-index: -1;
}

.border-glow-card:not(:hover):not(.sweep-active)::before,
.border-glow-card:not(:hover):not(.sweep-active)::after,
.border-glow-card:not(:hover):not(.sweep-active) > .edge-light {
  opacity: 0;
  transition: opacity 0.75s ease-in-out;
}

/* 网格渐变描边（随光标方向显隐） */
.border-glow-card::before {
  border: 1px solid transparent;
  background:
    linear-gradient(var(--card-bg, transparent) 0 100%) padding-box,
    linear-gradient(transparent 0% 100%) border-box,
    var(--gradient-one, transparent) border-box,
    var(--gradient-two, transparent) border-box,
    var(--gradient-three, transparent) border-box,
    var(--gradient-four, transparent) border-box,
    var(--gradient-five, transparent) border-box,
    var(--gradient-six, transparent) border-box,
    var(--gradient-seven, transparent) border-box,
    var(--gradient-base, transparent) border-box;

  opacity: calc((var(--edge-proximity) - var(--color-sensitivity)) / (100 - var(--color-sensitivity)));

  mask-image:
    conic-gradient(
      from var(--cursor-angle) at center,
      black calc(var(--cone-spread) * 1%),
      transparent calc((var(--cone-spread) + 15) * 1%),
      transparent calc((100 - var(--cone-spread) - 15) * 1%),
      black calc((100 - var(--cone-spread)) * 1%)
    );
}

/* 贴近边缘的网格渐变填充 */
.border-glow-card::after {
  border: 1px solid transparent;
  background:
    var(--gradient-one, transparent) padding-box,
    var(--gradient-two, transparent) padding-box,
    var(--gradient-three, transparent) padding-box,
    var(--gradient-four, transparent) padding-box,
    var(--gradient-five, transparent) padding-box,
    var(--gradient-six, transparent) padding-box,
    var(--gradient-seven, transparent) padding-box,
    var(--gradient-base, transparent) padding-box;

  mask-image:
    linear-gradient(to bottom, black, black),
    radial-gradient(ellipse at 50% 50%, black 40%, transparent 65%),
    radial-gradient(ellipse at 66% 66%, black 5%, transparent 40%),
    radial-gradient(ellipse at 33% 33%, black 5%, transparent 40%),
    radial-gradient(ellipse at 66% 33%, black 5%, transparent 40%),
    radial-gradient(ellipse at 33% 66%, black 5%, transparent 40%),
    conic-gradient(from var(--cursor-angle) at center, transparent 5%, black 15%, black 85%, transparent 95%);

  mask-composite: subtract, add, add, add, add, add;
  opacity: calc(var(--fill-opacity, 0.5) * (var(--edge-proximity) - var(--color-sensitivity)) / (100 - var(--color-sensitivity)));
  mix-blend-mode: soft-light;
}

/* 外发光层 */
.border-glow-card > .edge-light {
  inset: calc(var(--glow-padding) * -1);
  pointer-events: none;
  z-index: 1;

  mask-image:
    conic-gradient(
      from var(--cursor-angle) at center, black 2.5%, transparent 10%, transparent 90%, black 97.5%
    );

  opacity: calc((var(--edge-proximity) - var(--edge-sensitivity)) / (100 - var(--edge-sensitivity)));
  mix-blend-mode: plus-lighter;
}

.border-glow-card > .edge-light::before {
  content: '';
  position: absolute;
  inset: var(--glow-padding);
  border-radius: inherit;
  box-shadow:
    inset 0 0 0 1px var(--glow-color, transparent),
    inset 0 0 1px 0 var(--glow-color-60, transparent),
    inset 0 0 3px 0 var(--glow-color-50, transparent),
    inset 0 0 6px 0 var(--glow-color-40, transparent),
    inset 0 0 15px 0 var(--glow-color-30, transparent),
    inset 0 0 25px 2px var(--glow-color-20, transparent),
    inset 0 0 50px 2px var(--glow-color-10, transparent),
    0 0 1px 0 var(--glow-color-60, transparent),
    0 0 3px 0 var(--glow-color-50, transparent),
    0 0 6px 0 var(--glow-color-40, transparent),
    0 0 15px 0 var(--glow-color-30, transparent),
    0 0 25px 2px var(--glow-color-20, transparent),
    0 0 50px 2px var(--glow-color-10, transparent);
}

.border-glow-inner {
  display: flex;
  flex-direction: column;
  position: relative;
  overflow: auto;
  z-index: 1;
}
</style>
