import assert from 'node:assert/strict'
import { test } from 'node:test'

import { ActivityManager } from '../src/components/ui/activity/activityManager.ts'
import { IslandController } from '../src/components/ui/activity/islandController.ts'

test('IslandPlayground 核心生命周期：dev 命名空间注入与隔离清理', () => {
  const mgr = new ActivityManager()
  const ctrl = new IslandController(mgr)

  // 1. 模拟业务中已有真实活动
  mgr.register({
    id: 'crawler:job',
    source: 'crawler',
    kind: 'task',
    status: 'running',
    title: '真实价格采集任务',
    progress: 50,
  })

  // 2. 模拟实验室注入 dev 命名空间活动
  mgr.register({
    id: 'dev:crawl',
    source: 'crawler',
    kind: 'task',
    status: 'running',
    title: '模拟价格抓取 45%',
    progress: 45,
  })
  mgr.register({
    id: 'dev:updater',
    source: 'updater',
    kind: 'task',
    status: 'running',
    title: 'Holdexar v1.2.0 下载中',
    progress: 80,
  })

  assert.equal(mgr.count, 3)

  // 3. 执行仅清理 dev: 命名空间
  const devActs = mgr.activities.filter((a) => a.id.startsWith('dev:'))
  assert.equal(devActs.length, 2)
  for (const act of devActs) {
    mgr.remove(act.id)
  }

  // 4. 验证真实活动完全不受影响
  assert.equal(mgr.count, 1)
  assert.equal(mgr.activities[0].id, 'crawler:job')
  assert.equal(ctrl.primaryId, 'crawler:job')
})

test('IslandPlayground 重复 ID 原位更新：保持稳定，不重建或增殖活动', () => {
  const mgr = new ActivityManager()
  const ctrl = new IslandController(mgr)

  const first = mgr.register({
    id: 'dev:dup-test',
    source: 'test',
    kind: 'task',
    status: 'running',
    title: '初始任务 (20%)',
    progress: 20,
    priority: 30,
  })

  assert.ok(first)
  const initialCreated = first.createdAt
  assert.equal(ctrl.primaryId, 'dev:dup-test')
  assert.equal(ctrl.count, 1)

  // 再次传入相同 ID 更新进度
  const second = mgr.register({
    id: 'dev:dup-test',
    source: 'test',
    kind: 'task',
    status: 'running',
    title: '原位更新 (60%)',
    progress: 60,
    priority: 30,
  })

  assert.ok(second)
  assert.equal(ctrl.count, 1)
  assert.equal(second.createdAt, initialCreated)
  assert.equal(second.progress, 60)
  assert.equal(ctrl.primaryId, 'dev:dup-test')
})

test('IslandPlayground 乱序事件与突然结束：安全降级与平滑回退', () => {
  const mgr = new ActivityManager()
  const ctrl = new IslandController(mgr)

  mgr.register({
    id: 'dev:task-1',
    source: 'test',
    title: '任务 1',
    progress: 80,
    priority: 30,
  })
  mgr.register({
    id: 'dev:task-2',
    source: 'test',
    title: '任务 2',
    progress: 50,
    priority: 25,
  })

  assert.equal(ctrl.primaryId, 'dev:task-1')
  assert.equal(ctrl.secondaryId, 'dev:task-2')

  // 模拟乱序事件：任务 1 收到回退事件
  mgr.update('dev:task-1', { progress: 30, title: '任务 1 (乱序回退)' })
  assert.equal(mgr.get('dev:task-1')?.progress, 30)
  assert.equal(ctrl.primaryId, 'dev:task-1')

  // 模拟任务 1 突然结束（直接移除）
  mgr.remove('dev:task-1')
  assert.equal(ctrl.count, 1)
  // 原副岛 dev:task-2 自动接替成为唯一主岛
  assert.equal(ctrl.primaryId, 'dev:task-2')
  assert.equal(ctrl.secondaryId, null)
  assert.equal(ctrl.hasSecondary, false)
})

test('IslandPlayground 副岛直开后紧急状态仍能插队', () => {
  const mgr = new ActivityManager()
  const ctrl = new IslandController(mgr)

  mgr.register({
    id: 'dev:task-normal',
    source: 'test',
    title: '普通任务',
    priority: 30,
  })
  mgr.register({
    id: 'dev:task-secondary',
    source: 'test',
    title: '次要任务',
    priority: 20,
  })

  assert.equal(ctrl.primaryId, 'dev:task-normal')
  assert.equal(ctrl.secondaryId, 'dev:task-secondary')

  ctrl.swap()
  assert.equal(ctrl.primaryId, 'dev:task-normal')
  assert.equal(ctrl.secondaryId, null)
  assert.equal(ctrl.expandedId, 'dev:task-secondary')

  // 2. 突发紧急异常 (needsAttention: true)
  mgr.register({
    id: 'dev:alert',
    source: 'proxypool',
    kind: 'system',
    status: 'failed',
    needsAttention: true,
    priority: 99,
    title: '严重异常',
  })

  // 无论当前用户选了谁，needsAttention 强制插队成为主岛
  assert.equal(ctrl.primaryId, 'dev:alert')
  assert.equal(ctrl.expandedId, 'dev:task-secondary')
})
