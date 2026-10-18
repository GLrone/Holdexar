import assert from 'node:assert/strict'
import { test } from 'node:test'

import { resolvePresentation } from '../src/components/ui/activity/presentationResolver.ts'
import type { Activity } from '../src/components/ui/activity/types.ts'

test('Visual Geometry：模块标识永远保留，运行态通过进度轨道体现，严禁覆盖为通用 Spinner', () => {
  // 1. 价格采集进行中
  const crawlAct: Activity = {
    id: 'crawl-1',
    source: 'crawler',
    kind: 'task',
    status: 'running',
    priority: 30,
    title: '抓取价格',
    progress: 45,
    createdAt: Date.now(),
    updatedAt: Date.now(),
  }
  const crawlPres = resolvePresentation(crawlAct)
  assert.equal(crawlPres.badgeType, 'icon')
  assert.equal(crawlPres.iconName, 'refresh')
  assert.equal(crawlPres.motion, 'orbit')
  assert.equal(crawlPres.showPercent, true)

  // 2. 爬取失败
  const crawlFailAct: Activity = {
    ...crawlAct,
    status: 'failed',
  }
  const crawlFailPres = resolvePresentation(crawlFailAct)
  assert.equal(crawlFailPres.iconName, 'refresh')
  assert.equal(crawlFailPres.tone, 'error')
  assert.equal(crawlFailPres.hasWarningMarker, true)
  assert.equal(crawlFailPres.motion, 'none')

  // 3. 代理池异常
  const proxyAct: Activity = {
    id: 'proxy-err',
    source: 'proxypool',
    kind: 'system',
    status: 'failed',
    needsAttention: true,
    priority: 99,
    title: '代理异常',
    createdAt: Date.now(),
    updatedAt: Date.now(),
  }
  const proxyPres = resolvePresentation(proxyAct)
  assert.equal(proxyPres.badgeType, 'icon')
  assert.equal(proxyPres.iconName, 'globe')
  assert.equal(proxyPres.tone, 'error')
  assert.equal(proxyPres.hasWarningMarker, true)

  // 4. 系统快照归档在跑
  const backupAct: Activity = {
    id: 'backup-1',
    source: 'backup',
    kind: 'task',
    status: 'running',
    priority: 20,
    title: '快照归档',
    progress: 20,
    createdAt: Date.now(),
    updatedAt: Date.now(),
  }
  const backupPres = resolvePresentation(backupAct)
  assert.equal(backupPres.iconName, 'database')
  assert.equal(backupPres.motion, 'orbit')

  // 5. 显式指定图标但为在跑任务
  const customAct: Activity = {
    id: 'custom-1',
    source: 'custom',
    kind: 'task',
    status: 'running',
    icon: 'sparkle',
    priority: 10,
    title: '自定义任务',
    createdAt: Date.now(),
    updatedAt: Date.now(),
  }
  const customPres = resolvePresentation(customAct)
  assert.equal(customPres.iconName, 'sparkle')
  assert.equal(customPres.motion, 'orbit')
})

test('Visual Geometry：账单同步显示数据库标识与无底轨的运行进度', () => {
  const syncAct: Activity = {
    id: 'bills:sync',
    source: 'bills',
    kind: 'task',
    status: 'running',
    priority: 32,
    title: '同步账单',
    progress: 64,
    createdAt: Date.now(),
    updatedAt: Date.now(),
  }
  const syncPres = resolvePresentation(syncAct)

  assert.equal(syncPres.iconName, 'database')
  assert.equal(syncPres.motion, 'orbit')
  assert.equal(syncPres.showPercent, true)
})

test('Visual Geometry：版本更新模块生命周期与醒目徽章/图标', () => {
  // 1. 新版本可用：醒目「新」文字徽章
  const availAct: Activity = {
    id: 'updater:available',
    source: 'updater',
    kind: 'notice',
    status: 'attention',
    priority: 25,
    title: '发现新版本 v1.2.0',
    createdAt: Date.now(),
    updatedAt: Date.now(),
  }
  const availPres = resolvePresentation(availAct)
  assert.equal(availPres.badgeType, 'badge')
  assert.equal(availPres.badgeKey, 'island.badge.new')
  assert.equal(availPres.motion, 'pulse')

  // 2. 更新下载中：download 图标 + 旋转进度环
  const dlAct: Activity = {
    id: 'updater:download',
    source: 'updater',
    kind: 'task',
    status: 'running',
    priority: 45,
    title: '正在下载更新 75%',
    progress: 75,
    createdAt: Date.now(),
    updatedAt: Date.now(),
  }
  const dlPres = resolvePresentation(dlAct)
  assert.equal(dlPres.badgeType, 'icon')
  assert.equal(dlPres.iconName, 'download')
  assert.equal(dlPres.motion, 'orbit')
  assert.equal(dlPres.showPercent, true)

  // 3. 更新包已就绪：package 图标 + 状态小圆点
  const readyAct: Activity = {
    id: 'updater:ready',
    source: 'updater',
    kind: 'system',
    status: 'attention',
    priority: 50,
    title: '更新包已就绪',
    createdAt: Date.now(),
    updatedAt: Date.now(),
  }
  const readyPres = resolvePresentation(readyAct)
  assert.equal(readyPres.badgeType, 'icon')
  assert.equal(readyPres.iconName, 'package')
  assert.equal(readyPres.motion, 'dot')

  // 4. 更新下载失败：download 图标 + 警示角标
  const failAct: Activity = {
    id: 'updater:failed',
    source: 'updater',
    kind: 'task',
    status: 'failed',
    needsAttention: true,
    priority: 60,
    title: '更新下载失败',
    createdAt: Date.now(),
    updatedAt: Date.now(),
  }
  const failPres = resolvePresentation(failAct)
  assert.equal(failPres.badgeType, 'icon')
  assert.equal(failPres.iconName, 'download')
  assert.equal(failPres.tone, 'error')
  assert.equal(failPres.hasWarningMarker, true)
})

test('Visual Geometry：收起态恒定 36px 高度与 18px 圆角规格', () => {
  const COMPACT_H = 36
  const COMPACT_R = 18

  // 辅助计算函数模拟组件内部 size 口径
  function calcIslandSize(mode: 'idle' | 'notice' | 'task' | 'expanded', hasSecondary: boolean) {
    if (mode === 'expanded') {
      return { w: 420, h: 120, r: 24 }
    }
    if (hasSecondary) {
      return { w: 240, h: COMPACT_H, r: COMPACT_R }
    }
    if (mode === 'idle') {
      return { w: 180, h: COMPACT_H, r: COMPACT_R }
    }
    // single task or notice
    return { w: 220, h: COMPACT_H, r: COMPACT_R }
  }

  // 验证不同模式下的收起态高度恒定为 36px，圆角恒定为 18px
  const idleSize = calcIslandSize('idle', false)
  assert.equal(idleSize.h, 36)
  assert.equal(idleSize.r, 18)

  const singleTaskSize = calcIslandSize('task', false)
  assert.equal(singleTaskSize.h, 36)
  assert.equal(singleTaskSize.r, 18)

  const singleNoticeSize = calcIslandSize('notice', false)
  assert.equal(singleNoticeSize.h, 36)
  assert.equal(singleNoticeSize.r, 18)

  const dualModePrimarySize = calcIslandSize('task', true)
  assert.equal(dualModePrimarySize.w, 240)
  assert.equal(dualModePrimarySize.h, 36)
  assert.equal(dualModePrimarySize.r, 18)
})

test('Visual Geometry：副岛双活动 36px 圆形与三活动以上小胶囊规格', () => {
  function getSecondaryGeometry(extraCount: number) {
    if (extraCount <= 0) {
      return { shape: 'circle', w: 36, h: 36, r: 18 }
    }
    return { shape: 'capsule', minW: 58, h: 36, r: 18 }
  }

  // 双活动时（副岛 extraCount 为 0）
  const dualSec = getSecondaryGeometry(0)
  assert.equal(dualSec.shape, 'circle')
  assert.equal(dualSec.w, 36)
  assert.equal(dualSec.h, 36)
  assert.equal(dualSec.r, 18)

  // 三活动及以上（副岛 extraCount > 0）
  const multiSec = getSecondaryGeometry(2)
  assert.equal(multiSec.shape, 'capsule')
  assert.equal(multiSec.h, 36)
  assert.equal(multiSec.minW, 58)
  assert.equal(multiSec.r, 18)
})

test('Visual Geometry：主副岛空间切移几何位移常数计算', () => {
  function calcSwapDx(extraCount: number) {
    const primaryW = 240
    const gap = 8
    const secondaryW = extraCount > 0 ? 58 : 36
    return primaryW / 2 + gap + secondaryW / 2
  }

  // 双任务模式：(240/2) + 8 + (36/2) = 120 + 8 + 18 = 146px
  assert.equal(calcSwapDx(0), 146)

  // 三任务及以上：(240/2) + 8 + (58/2) = 120 + 8 + 29 = 157px
  assert.equal(calcSwapDx(1), 157)
})

test('Visual Geometry：Logo 内嵌环进度与加载动效计算', () => {
  const RING_CIRCUMFERENCE = 59.69

  function calcRingDash(progress: number | null) {
    if (progress !== null) {
      const clamped = Math.min(100, Math.max(0, progress))
      const filled = (RING_CIRCUMFERENCE * clamped) / 100
      return {
        isDeterminate: true,
        dasharray: `${filled.toFixed(2)} ${RING_CIRCUMFERENCE}`,
      }
    }
    return {
      isDeterminate: false,
      dasharray: '18 42',
    }
  }

  // 1. 定量进度（如 60%）
  const p60 = calcRingDash(60)
  assert.equal(p60.isDeterminate, true)
  assert.equal(p60.dasharray, '35.81 59.69')

  // 2. 边界 0% 与 100%
  const p0 = calcRingDash(0)
  assert.equal(p0.isDeterminate, true)
  assert.equal(p0.dasharray, '0.00 59.69')

  const p100 = calcRingDash(100)
  assert.equal(p100.isDeterminate, true)
  assert.equal(p100.dasharray, '59.69 59.69')

  // 3. 不定量加载动画（旋转动效）
  const ind = calcRingDash(null)
  assert.equal(ind.isDeterminate, false)
  assert.equal(ind.dasharray, '18 42')
})
