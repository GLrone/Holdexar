import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

import { createRectKeyframes, islandSwapDuration, islandSwapEasing } from '../src/components/ui/activity/islandMotionGeometry.ts'

const islandSource = readFileSync(new URL('../src/components/ui/HlIsland.vue', import.meta.url), 'utf8')
const motionSource = readFileSync(new URL('../src/components/ui/activity/islandLayoutMotion.ts', import.meta.url), 'utf8')
const secondarySource = readFileSync(new URL('../src/components/ui/activity/SecondaryIsland.vue', import.meta.url), 'utf8')
const markSource = readFileSync(new URL('../src/components/ui/activity/ActivityMark.vue', import.meta.url), 'utf8')
const itemSource = readFileSync(new URL('../src/components/ui/activity/ActivityItem.vue', import.meta.url), 'utf8')
const primarySource = readFileSync(new URL('../src/components/ui/activity/PrimaryIsland.vue', import.meta.url), 'utf8')
const activityManagerSource = readFileSync(new URL('../src/components/ui/activity/activityManager.ts', import.meta.url), 'utf8')
const playgroundSource = readFileSync(new URL('../src/components/ui/activity/IslandPlayground.vue', import.meta.url), 'utf8')
const activityPanelSource = readFileSync(new URL('../src/components/ui/activity/ActivityPanel.vue', import.meta.url), 'utf8')
const tokenSource = readFileSync(new URL('../src/styles/tokens.css', import.meta.url), 'utf8')
const lightTokenSource = tokenSource.match(/:root\s*\{([\s\S]*?)\n\}/)?.[1] ?? ''
const darkTokenSource = tokenSource.match(/html\.dark\s*\{([\s\S]*?)\n\}/)?.[1] ?? ''
const islandStyle = islandSource.match(/<style scoped>([\s\S]*?)<\/style>/)?.[1] ?? ''

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

test('island motion source：槽位容器与任务标识分别连续迁移', () => {
  assert.match(motionSource, /getBoundingClientRect\(\)/)
  assert.match(motionSource, /\.animate\(/)
  assert.match(motionSource, /\.finished/)
  assert.match(motionSource, /data-activity-logo/)
  assert.match(islandSource, /data-island-slot="expanded"/)
  assert.match(islandSource, /islandController\.openSecondary\(\)/)
  assert.doesNotMatch(islandSource, /isSwapBusy|swapSequence|swapTimer[12]|swapDistance|is-phase-sliding|is-phase-settling/)
  assert.doesNotMatch(motionSource, /setTimeout|setInterval/)
  assert.doesNotMatch(secondarySource, /is-phase-sliding|is-phase-settling|hl-secondary-settle|translateY\(4px\)/)
  assert.doesNotMatch(secondarySource, /\.hl-secondary-island:hover\s*\{[^}]*transform:\s*scale/s)
  assert.match(primarySource, /returnToPrevious/)
  assert.match(primarySource, /emit\(['"]collapse['"]\)/)
})

test('island motion overlays：候补动画副本清理后不会留成重复槽位', () => {
  assert.match(motionSource, /clone\.removeAttribute\(['"]data-island-slot['"]\)/)
  assert.match(motionSource, /private readonly overlays = new Set<HTMLElement>\(\)/)
  assert.match(motionSource, /this\.overlays\.add\(shellClone\)/)
  assert.match(motionSource, /this\.overlays\.add\(logoClone\)/)
  assert.match(motionSource, /for \(const overlay of this\.overlays\)\s*overlay\.remove\(\)/)
})

test('island motion transition：内容随几何壳层同轨迹移动并在壳层内裁切交接', () => {
  assert.match(primarySource, /data-island-content/)
  assert.doesNotMatch(primarySource, /<Transition name="island-content-switch" mode="out-in">/)
  assert.doesNotMatch(primarySource, /island-content-switch-(?:enter|leave)/)
  assert.match(motionSource, /data-island-content/)
  assert.match(motionSource, /textOffsetOf/)
  assert.match(motionSource, /shellClone\.appendChild\(source\.content\)/)
  assert.match(motionSource, /shellClone\.appendChild\(contentClone\)/)
  assert.match(motionSource, /source\.content\.animate\(/)
  assert.match(motionSource, /contentClone\.animate\(/)
  assert.match(motionSource, /translate\(\$\{deltaX\}px, \$\{deltaY\}px\)/)
  assert.match(motionSource, /translate\(\$\{-deltaX\}px, \$\{-deltaY\}px\)/)
  assert.match(motionSource, /setTemporaryStyle\(target\.shell, 'background-color', 'transparent'\)/)
  assert.doesNotMatch(motionSource, /target\.shell\.style\.opacity\s*=\s*['"]0['"]?/)
})

test('island mark geometry：展开后的进度环跟随完整标识尺寸', () => {
  assert.match(markSource, /\.hl-activity-mark__orbit\s*\{[^}]*width:\s*100%[^}]*height:\s*100%/s)
  assert.match(markSource, /transform-box:\s*view-box/)
  assert.match(markSource, /transform-origin:\s*50%\s+50%/)
})

test('island controls：实验室触发副岛直开且主活动列表不用边框提示选中', () => {
  assert.match(playgroundSource, /islandController\.requestSwap\(\)/)
  assert.ok(playgroundSource.includes('setExpanded(expanded)'))
  assert.ok(playgroundSource.includes('function toggleIslandExpanded()'))
  assert.doesNotMatch(itemSource, /\.hl-activity-item\.is-primary\s*\{[^}]*border\s*:/s)
  assert.doesNotMatch(islandSource, /@after-close="onActivityPanelAfterLeave"/)
  assert.match(islandSource, /@return="returnToPreviousLayout"/)
  assert.match(islandSource, /@collapse="collapseExpanded"/)
})

test('island controls：活动提示不提供类别静音入口', () => {
  assert.doesNotMatch(primarySource, /island\.mute|emit\(['"]mute['"]\)/)
  assert.doesNotMatch(islandSource, /\bmuteTone\b|\.muted\b/)
  assert.doesNotMatch(activityManagerSource, /_mutedTones|muteTone|unmuteTone/)
})

test('idle island status：空闲价格状态可关闭并在新活动到达后恢复', () => {
  assert.match(primarySource, /v-if="showingPrice && canDismissStatus"[\s\S]*?t\('island\.hideStatus'\)[\s\S]*?emit\('dismiss'\)/)
  assert.match(islandSource, /:can-dismiss-status="!islandController\.isExpanded\.value"/)
  assert.match(islandSource, /:can-dismiss-status="true"/)
  assert.match(islandSource, /const idleStatusDismissed = ref\(false\)/)
  assert.match(islandSource, /!idleStatusDismissed\.value &&/)
  assert.match(islandSource, /if \(showingPrice\.value\) \{[\s\S]*?idleStatusDismissed\.value = true/)
  assert.match(islandSource, /watch\([\s\S]*?idleStatusDismissed\.value = false/)
})

test('idle price expansion：紧凑岛体直接形变为卡片且不重复保留主岛', () => {
  const compactIsland = islandSource.match(/<div[\s\S]*?data-island-slot="main"[\s\S]*?>/)?.[0] ?? ''

  assert.match(islandSource, /const showCompactMain = computed\(\(\) => !\(showingPrice\.value && islandController\.isIdleExpanded\)\)/)
  assert.match(compactIsland, /v-if="showCompactMain"/)
  assert.match(islandSource, /:data-activity-id="mainDataId"[\s\S]*?data-island-slot="main"/)
  assert.match(islandSource, /:data-activity-id="islandController\.expandedId"[\s\S]*?data-island-slot="expanded"/)
  assert.match(islandSource, /'is-idle-expanded': expandedShowingPrice/)
  assert.match(islandStyle, /\.hl-expanded-island\.is-idle-expanded\s*\{[^}]*top:\s*var\(--island-top\)/s)
})

test('expanded main interaction：只触发 Main 与 Expanded 身份交换', () => {
  const mainInteraction = islandSource.match(/function onIslandClick\(\) \{([\s\S]*?)\n\}/)?.[1] ?? ''
  assert.match(mainInteraction, /islandController\.openMain\(\)/)
  assert.doesNotMatch(mainInteraction, /openSecondary|requestSwap/)
})

test('island text：岛体及后代均禁止鼠标拖动选中文字', () => {
  const hostRule = islandStyle.match(/\.hl-island-host\s*\{([\s\S]*?)\n\}/)?.[1] ?? ''
  const descendantRule = islandStyle.match(/\.hl-island-host\s*:deep\(\*\)\s*\{([\s\S]*?)\n\}/)?.[1] ?? ''

  assert.match(hostRule, /user-select:\s*none/)
  assert.match(hostRule, /-webkit-user-select:\s*none/)
  assert.match(descendantRule, /user-select:\s*none/)
  assert.match(descendantRule, /-webkit-user-select:\s*none/)
})

test('island transition：测量副本独占几何动效并禁用内容子动效', () => {
  const targetSetup = motionSource.match(/const shellClone = source\?\.shell[\s\S]*?if \(target\.content\)/)?.[0] ?? ''

  assert.match(motionSource, /this\.setTemporaryStyle\(target\.shell,\s*['"]transition['"],\s*['"]none['"]\)/)
  assert.match(targetSetup, /this\.setTemporaryStyle\(target\.shell,\s*['"]background-color['"],\s*['"]transparent['"]\)/)
  assert.match(targetSetup, /if \(target\.logo\) this\.setTemporaryStyle\(target\.logo,\s*['"]opacity['"],\s*['"]0['"]\)/)
  assert.match(motionSource, /kind === ['"]content['"][\s\S]*?querySelectorAll<HTMLElement>\(['"]\*['"]\)/)
  assert.match(motionSource, /setProperty\(['"]animation['"],\s*['"]none['"]\)/)
  assert.match(motionSource, /setProperty\(['"]transition['"],\s*['"]none['"]\)/)
})

test('island dark theme：岛体与页面保持可见层次且不用描边', () => {
  const readColor = (name) => darkTokenSource.match(new RegExp(`${name}:\\s*(#[0-9a-fA-F]{6})`))?.[1]
  const background = readColor('--bg-base')
  const island = readColor('--island-shell-bg')
  assert.ok(background && island)
  assert.equal(island, '#090a0d')
  assert.equal(lightTokenSource.match(/--island-shell-bg:\s*(#[0-9a-fA-F]{6})/)?.[1], '#090a0d')
  assert.equal(readColor('--island-shell-fg'), '#f5f6f8')
  assert.equal(lightTokenSource.match(/--island-shell-fg:\s*(#[0-9a-fA-F]{6})/)?.[1], '#f5f6f8')
  const islandShadow = darkTokenSource.match(/--shadow-island:\s*([\s\S]*?);/)?.[1] ?? ''
  assert.doesNotMatch(islandShadow, /color-mix|var\(--accent\)/)
  assert.match(islandSource, /\.hl-island\s*\{[^}]*border:\s*none/s)
  assert.match(islandSource, /\.hl-island\s*\{[^}]*color:\s*var\(--island-shell-fg\)/s)
  assert.match(islandSource, /:deep\(\.hl-secondary-island\)\s*\{[^}]*background:\s*var\(--island-shell-bg\)/s)
})

test('activity overview：列表与展开卡片共用页面正中主岛锚点，并快速响应选择和关闭', () => {
  const hostRule = islandStyle.match(/\.hl-island-host\s*\{([\s\S]*?)\n\}/)?.[1] ?? ''
  const expandedRule = islandStyle.match(/\.hl-expanded-island\s*\{([\s\S]*?)\n\}/)?.[1] ?? ''
  const islandPanelStyle = activityPanelSource.match(/\.hl-activity-panel\s*\{([\s\S]*?)\n\}/)?.[1] ?? ''
  const panelTransition = activityPanelSource.match(/\.activity-panel-enter-active,[\s\S]*?\n\}/)?.[0] ?? ''
  const selectionHandler = islandSource.match(/function onSelectActivity\(id: string\) \{([\s\S]*?)\n\}/)?.[1] ?? ''

  assert.doesNotMatch(islandSource, /--island-anchor-shift|--island-anchor-inset/)
  assert.doesNotMatch(activityPanelSource, /--island-anchor-shift|--island-anchor-inset/)
  assert.match(hostRule, /left:\s*50%/)
  assert.match(expandedRule, /left:\s*50%/)
  assert.match(islandSource, /width:\s*'min\(420px, calc\(100vw - 24px\)\)'/)
  assert.match(islandPanelStyle, /left:\s*0/)
  assert.match(islandPanelStyle, /right:\s*auto/)
  assert.match(islandPanelStyle, /transform:\s*translateX\(-50%\)/)
  assert.match(islandPanelStyle, /width:\s*min\(320px, calc\(100vw - 24px\)\)/)
  assert.match(islandPanelStyle, /z-index:\s*10003/)
  assert.match(panelTransition, /var\(--duration-1\)/)
  assert.match(selectionHandler, /transitionLayout\(\(\)\s*=>\s*\{[\s\S]*?setOverview\(false\)[\s\S]*?selectPrimary\(id\)/)
  assert.doesNotMatch(islandSource, /pendingSelectionId|onActivityPanelAfterLeave/)
})

test('island playground：动态进度预设接入连续状态更新', () => {
  const progressPreset = playgroundSource.match(/case 'progress':([\s\S]*?)case 'alert':/)?.[1] ?? ''
  assert.match(progressPreset, /startContinuousProgress\(/)
  assert.doesNotMatch(progressPreset, /progress:\s*15/)
  assert.match(playgroundSource, /function startContinuousProgress\([\s\S]*?setInterval\([\s\S]*?activityManager\.update\(/)
  assert.match(progressPreset, /completionTitle:\s*t\('island\.playground\.mockDownloadDoneTitle'\)/)
  assert.match(playgroundSource, /completionTitle:\s*string[\s\S]*?activityManager\.complete\([\s\S]*?title:\s*options\.completionTitle/)
})
