<script setup lang="ts">
import { onMounted, ref } from 'vue'

export interface HlGooeyNavItem {
  label: string
  value?: string
}

const props = withDefaults(defineProps<{ items: HlGooeyNavItem[] }>(), {
  items: () => [],
})

const model = defineModel<number>({ default: 0 })

const COLORS = [
  [1, 2],
  [3, 4],
  [5, 6],
  [7, 8],
  [1, 2],
]
const COLOR_VARS: Record<number, string> = {
  1: '#66c0f4',
  2: '#1a9fff',
  3: '#a4ce3d',
  4: '#60a820',
  5: '#ab57dd',
  6: '#782bb0',
  7: '#e5e4e2',
  8: '#8f98a0',
}

const container = ref<HTMLElement | null>(null)
const listEl = ref<HTMLUListElement | null>(null)
const filterEl = ref<HTMLElement | null>(null)
const textEl = ref<HTMLElement | null>(null)

function noise(n: number) {
  return n / 2 - Math.random() * n
}

function getXY(dist: number, i: number, total: number) {
  const angle = (((360 + noise(8)) / total) * i * Math.PI) / 180
  return [dist * Math.cos(angle), dist * Math.sin(angle)]
}

function updatePos(el: HTMLElement) {
  const cr = container.value!.getBoundingClientRect()
  const r = el.getBoundingClientRect()
  const s = {
    left: `${r.left - cr.left}px`,
    top: `${r.top - cr.top}px`,
    width: `${r.width}px`,
    height: `${r.height}px`,
  }
  Object.assign(filterEl.value!.style, s)
  Object.assign(textEl.value!.style, s)
  textEl.value!.textContent = el.textContent
}

function spawnParticle(cls: string, vars: Record<string, string>, ttl: number) {
  const p = document.createElement('span')
  p.className = cls
  for (const [k, v] of Object.entries(vars)) p.style.setProperty(k, v)
  const pt = document.createElement('span')
  pt.className = cls.includes('venom') ? 'hl-gn-point hl-gn-venom-point' : 'hl-gn-point'
  p.appendChild(pt)
  filterEl.value!.appendChild(p)
  filterEl.value!.classList.add('is-active')
  setTimeout(() => {
    try {
      filterEl.value!.removeChild(p)
    } catch {
      /* 已被清理 */
    }
  }, ttl)
}

/** 相邻切换 = venom 抛物粒子；跳切 = 环形散射 */
function makeParticles(targetIdx: number, oldIdx?: number) {
  const items = listEl.value?.querySelectorAll('li') ?? []
  const isAdjacent = oldIdx !== undefined && Math.abs(targetIdx - oldIdx) === 1
  const colorArr = COLORS[targetIdx % COLORS.length]
  filterEl.value!.innerHTML = ''
  if (isAdjacent && items[oldIdx!] && items[targetIdx]) {
    const or = items[oldIdx!].getBoundingClientRect()
    const nr = items[targetIdx].getBoundingClientRect()
    const dX = or.left - nr.left + (or.width - nr.width) / 2
    const dY = or.top - nr.top + (or.height - nr.height) / 2
    for (let i = 0; i < 24; i++) {
      const t = 300 + Math.random() * 150
      const color = colorArr[Math.floor(Math.random() * colorArr.length)]
      const spread = 80
      const sx = dX + noise(spread)
      const sy = dY + noise(spread * 0.6)
      const ex = noise(15)
      const ey = noise(15)
      const mx = (sx + ex) / 2 + noise(spread * 1.2)
      const my = (sy + ey) / 2 + noise(spread * 0.8)
      const delay = Math.random() * 250
      setTimeout(() => {
        spawnParticle(
          'hl-gn-particle hl-gn-venom',
          {
            '--start-x': `${sx}px`,
            '--start-y': `${sy}px`,
            '--mid-x': `${mx}px`,
            '--mid-y': `${my}px`,
            '--end-x': `${ex}px`,
            '--end-y': `${ey}px`,
            '--time': `${t}ms`,
            '--scale': `${1 + noise(0.2)}`,
            '--color': COLOR_VARS[color] ?? '#fff',
          },
          t,
        )
      }, 30 + delay)
    }
  } else {
    for (let i = 0; i < 30; i++) {
      const t = 400 + Math.random() * 200
      const start = getXY(90, 30 - i, 30)
      const end = getXY(10 + noise(7), 30 - i, 30)
      const color = colorArr[Math.floor(Math.random() * colorArr.length)]
      const rot = (noise(5) > 0 ? noise(5) + 2.5 : noise(5) - 2.5) * 10
      setTimeout(() => {
        spawnParticle(
          'hl-gn-particle',
          {
            '--start-x': `${start[0]}px`,
            '--start-y': `${start[1]}px`,
            '--end-x': `${end[0]}px`,
            '--end-y': `${end[1]}px`,
            '--time': `${t}ms`,
            '--scale': `${1 + noise(0.2)}`,
            '--rotate': `${rot}deg`,
            '--color': COLOR_VARS[color] ?? '#fff',
          },
          t + 50,
        )
      }, 30)
    }
  }
}

function pick(idx: number) {
  if (idx === model.value) return
  const oldIdx = model.value
  model.value = idx
  const items = listEl.value?.querySelectorAll('li') ?? []
  items.forEach((b, i) => b.classList.toggle('is-active', i === idx))
  if (items[idx]) {
    updatePos(items[idx])
    textEl.value?.classList.remove('is-active')
    void textEl.value?.offsetWidth
    textEl.value?.classList.add('is-active')
  }
  makeParticles(idx, oldIdx)
}

onMounted(() => {
  const items = listEl.value?.querySelectorAll('li') ?? []
  if (items[model.value]) {
    updatePos(items[model.value])
    textEl.value?.classList.add('is-active')
  }
})
</script>

<template>
  <div ref="container" class="hl-gooey-nav">
    <svg width="0" height="0" style="position: absolute" aria-hidden="true">
      <defs>
        <filter id="hl-gooey-nav-filter">
          <feGaussianBlur in="SourceGraphic" stdDeviation="18" result="blur" />
          <feColorMatrix
            in="blur"
            mode="matrix"
            values="1 0 0 0 0  0 1 0 0 0  0 0 1 0 0  0 0 0 35 -15"
            result="goo"
          />
          <feComposite in="SourceGraphic" in2="goo" operator="atop" />
        </filter>
      </defs>
    </svg>
    <nav>
      <ul ref="listEl">
        <li
          v-for="(item, i) in items"
          :key="i"
          :class="{ 'is-active': model === i }"
          @click="pick(i)"
        >
          <span>{{ item.label }}</span>
        </li>
      </ul>
      <div ref="filterEl" class="hl-gn-effect hl-gn-effect-filter" />
      <div ref="textEl" class="hl-gn-effect">
        <span class="hl-gn-effect-text" />
      </div>
    </nav>
  </div>
</template>
