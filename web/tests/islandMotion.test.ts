import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

import { createRectKeyframes, islandSwapDuration, islandSwapEasing } from '../src/components/ui/activity/islandMotionGeometry.ts'

const islandSource = readFileSync(new URL('../src/components/ui/HlIsland.vue', import.meta.url), 'utf8')
const secondarySource = readFileSync(new URL('../src/components/ui/activity/SecondaryIsland.vue', import.meta.url), 'utf8')
const markSource = readFileSync(new URL('../src/components/ui/activity/ActivityMark.vue', import.meta.url), 'utf8')
const itemSource = readFileSync(new URL('../src/components/ui/activity/ActivityItem.vue', import.meta.url), 'utf8')
const primarySource = readFileSync(new URL('../src/components/ui/activity/PrimaryIsland.vue', import.meta.url), 'utf8')
const activityManagerSource = readFileSync(new URL('../src/components/ui/activity/activityManager.ts', import.meta.url), 'utf8')
const playgroundSource = readFileSync(new URL('../src/components/ui/activity/IslandPlayground.vue', import.meta.url), 'utf8')
const tokenSource = readFileSync(new URL('../src/styles/tokens.css', import.meta.url), 'utf8')
const darkTokenSource = tokenSource.match(/html\.dark\s*\{([\s\S]*?)\n\}/)?.[1] ?? ''

test('island motion geometry：动画只按测得的矩形连续移动', () => {
  const frames = createRectKeyframes(
    { left: 520, top: 14, width: 36, height: 36, borderRadius: '18px' },
    { left: 270, top: 14, width: 240, height: 36, borderRadius: '18px' },
  )

  assert.deepEqual(frames, [
    { left: '520px', top: '14px', width: '36px', height: '36px', borderRadius: '18px', opacity: 1 },
    { left: '270px', top: '14px', width: '240px', height: '36px', borderRadius: '18px', opacity: 1 },
  ])
  assert.equal(islandSwapDuration, 320)
  assert.equal(islandSwapEasing, 'cubic-bezier(0.22, 1, 0.36, 1)')
})

test('island motion source：交换不再用固定宽度、双阶段计时或回弹位移', () => {
  assert.match(islandSource, /getBoundingClientRect\(\)/)
  assert.match(islandSource, /\.animate\(/)
  assert.match(islandSource, /\.finished/)
  assert.doesNotMatch(islandSource, /swapTimer[12]|swapDistance|is-phase-sliding|is-phase-settling/)
  assert.doesNotMatch(secondarySource, /is-phase-sliding|is-phase-settling|hl-secondary-settle|translateY\(4px\)/)
})

test('island mark geometry：展开后的进度环跟随完整标识尺寸', () => {
  assert.match(markSource, /\.hl-activity-mark__orbit\s*\{[^}]*width:\s*100%[^}]*height:\s*100%/s)
  assert.match(markSource, /transform-box:\s*view-box/)
  assert.match(markSource, /transform-origin:\s*50%\s+50%/)
})

test('island controls：实验室触发真实交换且主活动列表不用边框提示选中', () => {
  assert.match(playgroundSource, /islandController\.requestSwap\(\)/)
  assert.ok(playgroundSource.includes('setExpanded(expanded)'))
  assert.ok(playgroundSource.includes('function toggleIslandExpanded()'))
  assert.doesNotMatch(itemSource, /\.hl-activity-item\.is-primary\s*\{[^}]*border\s*:/s)
  assert.match(islandSource, /@after-close="onActivityPanelAfterLeave"/)
})

test('island controls：活动提示不提供类别静音入口', () => {
  assert.doesNotMatch(primarySource, /island\.mute|emit\(['"]mute['"]\)/)
  assert.doesNotMatch(islandSource, /\bmuteTone\b|\.muted\b/)
  assert.doesNotMatch(activityManagerSource, /_mutedTones|muteTone|unmuteTone/)
})

test('island dark theme：岛体与页面保持可见层次且不用描边', () => {
  const readColor = (name) => darkTokenSource.match(new RegExp(`${name}:\\s*(#[0-9a-fA-F]{6})`))?.[1]
  const background = readColor('--bg-base')
  const island = readColor('--island-bg')
  assert.ok(background && island)

  const luminance = (hex) => {
    const channels = [1, 3, 5].map((start) => parseInt(hex.slice(start, start + 2), 16) / 255)
    const linear = channels.map((value) => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4)
    return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722
  }
  const first = luminance(background)
  const second = luminance(island)
  const contrast = (Math.max(first, second) + 0.05) / (Math.min(first, second) + 0.05)

  assert.ok(contrast >= 1.6, `dark island contrast was ${contrast.toFixed(2)}`)
  assert.match(darkTokenSource, /--shadow-island:[\s\S]*?color-mix\(in srgb, var\(--accent\)/)
  assert.match(islandSource, /\.hl-island\s*\{[^}]*border:\s*none/s)
})

test('island playground：动态进度预设接入连续状态更新', () => {
  const progressPreset = playgroundSource.match(/case 'progress':([\s\S]*?)case 'alert':/)?.[1] ?? ''
  assert.match(progressPreset, /startContinuousProgress\(/)
  assert.doesNotMatch(progressPreset, /progress:\s*15/)
  assert.match(playgroundSource, /function startContinuousProgress\([\s\S]*?setInterval\([\s\S]*?activityManager\.update\(/)
  assert.match(progressPreset, /completionTitle:\s*t\('island\.playground\.mockDownloadDoneTitle'\)/)
  assert.match(playgroundSource, /completionTitle:\s*string[\s\S]*?activityManager\.complete\([\s\S]*?title:\s*options\.completionTitle/)
})
