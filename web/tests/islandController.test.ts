import assert from 'node:assert/strict'
import { test } from 'node:test'

import { ActivityManager } from '../src/components/ui/activity/activityManager.ts'
import { IslandController } from '../src/components/ui/activity/islandController.ts'

test('IslandController：紧急关注项置顶优先展示', () => {
  const mgr = new ActivityManager()
  const ctrl = new IslandController(mgr)

  mgr.register({
    id: 'crawl-task',
    source: 'crawler',
    kind: 'task',
    status: 'running',
    priority: 30,
    title: '正在抓取',
  })

  assert.equal(ctrl.primaryId, 'crawl-task')
  assert.equal(ctrl.secondaryId, null)

  // 注入严重异常项
  mgr.register({
    id: 'proxy-err',
    source: 'proxypool',
    kind: 'notice',
    status: 'failed',
    needsAttention: true,
    title: '代理全失效',
  })

  // 紧急项立即抢占主岛
  assert.equal(ctrl.primaryId, 'proxy-err')
  assert.equal(ctrl.secondaryId, 'crawl-task')
})

test('IslandController：普通后台任务进度刷新不抢占用户选定主岛', () => {
  const mgr = new ActivityManager()
  const ctrl = new IslandController(mgr)

  mgr.register({
    id: 'updater-task',
    source: 'updater',
    kind: 'task',
    status: 'running',
    priority: 30,
    title: '下载安装包',
  })

  mgr.register({
    id: 'crawl-task',
    source: 'crawler',
    kind: 'task',
    status: 'running',
    priority: 30,
    title: '抓取价格',
  })

  // 用户点击副岛切换或手动选定 updater 为主岛
  ctrl.selectPrimary('updater-task')
  assert.equal(ctrl.primaryId, 'updater-task')
  assert.equal(ctrl.secondaryId, 'crawl-task')

  // crawl 任务在后台不断更新进度
  mgr.update('crawl-task', { progress: 20, title: '抓取 20%' })
  mgr.update('crawl-task', { progress: 40, title: '抓取 40%' })

  // 主岛依然保持用户选定的 updater，未被抢占
  assert.equal(ctrl.primaryId, 'updater-task')
  assert.equal(ctrl.secondaryId, 'crawl-task')
})

test('IslandController：点击副岛交换主副岛', () => {
  const mgr = new ActivityManager()
  const ctrl = new IslandController(mgr)

  mgr.register({ id: 'task-a', source: 'bills', title: '任务 A', priority: 30 })
  mgr.register({ id: 'task-b', source: 'cdk', title: '任务 B', priority: 25 })

  assert.equal(ctrl.primaryId, 'task-a')
  assert.equal(ctrl.secondaryId, 'task-b')

  ctrl.swap()

  assert.equal(ctrl.primaryId, 'task-b')
  assert.equal(ctrl.secondaryId, 'task-a')
})

test('IslandController：活动消失后安全回退且无悬空指针', () => {
  const mgr = new ActivityManager()
  const ctrl = new IslandController(mgr)

  mgr.register({ id: 'task-1', source: 'a', title: '任务 1' })
  mgr.register({ id: 'task-2', source: 'b', title: '任务 2' })

  assert.equal(ctrl.primaryId, 'task-1')
  assert.equal(ctrl.secondaryId, 'task-2')

  // 移除任务 1
  mgr.remove('task-1')

  // 主岛自动回退到任务 2，副岛置空，无悬空 ID
  assert.equal(ctrl.primaryId, 'task-2')
  assert.equal(ctrl.secondaryId, null)

  // 移除任务 2
  mgr.remove('task-2')
  assert.equal(ctrl.primaryId, null)
  assert.equal(ctrl.secondaryId, null)
})

test('IslandController：固定主岛防止新任务排入顶替', () => {
  const mgr = new ActivityManager()
  const ctrl = new IslandController(mgr)

  mgr.register({ id: 'pinned-job', source: 'a', title: '常驻监控', priority: 20 })
  ctrl.pin('pinned-job')
  assert.equal(ctrl.pinnedId, 'pinned-job')

  // 加入更高优先级的普通任务
  mgr.register({ id: 'high-job', source: 'b', title: '新高优先级', priority: 40 })

  // 因为固定，主岛仍为 pinned-job，新任务排为副岛
  assert.equal(ctrl.primaryId, 'pinned-job')
  assert.equal(ctrl.secondaryId, 'high-job')

  // 解除固定后，更高优先级任务正常升为主岛
  ctrl.unpin()
  mgr.update('high-job', { progress: 10 }) // 触发一次队列评估
  assert.equal(ctrl.primaryId, 'high-job')
})

test('IslandController：三任务轮转与副岛轮转队列稳定', () => {
  const mgr = new ActivityManager()
  const ctrl = new IslandController(mgr)

  mgr.register({ id: 'task-a', source: 'a', title: '任务 A', priority: 30 })
  mgr.register({ id: 'task-b', source: 'b', title: '任务 B', priority: 30 })
  mgr.register({ id: 'task-c', source: 'c', title: '任务 C', priority: 30 })

  // 初始：A 为主岛，B 为副岛，等待数为 1 (+1)
  assert.equal(ctrl.primaryId, 'task-a')
  assert.equal(ctrl.secondaryId, 'task-b')
  assert.equal(ctrl.waitingCount, 1)

  // 第一次点击副岛：B 升为主岛，C 成为副岛，A 重新排入队尾
  ctrl.swap()
  assert.equal(ctrl.primaryId, 'task-b')
  assert.equal(ctrl.secondaryId, 'task-c')
  assert.equal(ctrl.waitingCount, 1)

  // 第二次点击副岛：C 升为主岛，A 成为副岛，B 重新排入队尾
  ctrl.swap()
  assert.equal(ctrl.primaryId, 'task-c')
  assert.equal(ctrl.secondaryId, 'task-a')
  assert.equal(ctrl.waitingCount, 1)

  // 第三次点击副岛：回到 A 主岛，B 副岛
  ctrl.swap()
  assert.equal(ctrl.primaryId, 'task-a')
  assert.equal(ctrl.secondaryId, 'task-b')
  assert.equal(ctrl.waitingCount, 1)
})

test('IslandController：四任务连续轮转顺畅', () => {
  const mgr = new ActivityManager()
  const ctrl = new IslandController(mgr)

  mgr.register({ id: 't1', source: 's1', title: 'T1', priority: 20 })
  mgr.register({ id: 't2', source: 's2', title: 'T2', priority: 20 })
  mgr.register({ id: 't3', source: 's3', title: 'T3', priority: 20 })
  mgr.register({ id: 't4', source: 's4', title: 'T4', priority: 20 })

  assert.equal(ctrl.primaryId, 't1')
  assert.equal(ctrl.secondaryId, 't2')
  assert.equal(ctrl.waitingCount, 2)

  // 轮转 T1 -> T2 -> T3 -> T4 -> T1
  ctrl.swap()
  assert.equal(ctrl.primaryId, 't2')
  assert.equal(ctrl.secondaryId, 't3')

  ctrl.swap()
  assert.equal(ctrl.primaryId, 't3')
  assert.equal(ctrl.secondaryId, 't4')

  ctrl.swap()
  assert.equal(ctrl.primaryId, 't4')
  assert.equal(ctrl.secondaryId, 't1')

  ctrl.swap()
  assert.equal(ctrl.primaryId, 't1')
  assert.equal(ctrl.secondaryId, 't2')
})

test('IslandController：多任务动态增删与自然收缩回单岛/空闲', () => {
  const mgr = new ActivityManager()
  const ctrl = new IslandController(mgr)

  // 0 -> 1: 单岛
  mgr.register({ id: 't1', source: 's1', title: 'T1' })
  assert.equal(ctrl.count, 1)
  assert.equal(ctrl.hasSecondary, false)
  assert.equal(ctrl.primaryId, 't1')
  assert.equal(ctrl.secondaryId, null)

  // 1 -> 2: 双岛出现
  mgr.register({ id: 't2', source: 's2', title: 'T2' })
  assert.equal(ctrl.count, 2)
  assert.equal(ctrl.hasSecondary, true)
  assert.equal(ctrl.waitingCount, 0)
  assert.equal(ctrl.primaryId, 't1')
  assert.equal(ctrl.secondaryId, 't2')

  // 2 -> 3: 计数提示出现
  mgr.register({ id: 't3', source: 's3', title: 'T3' })
  assert.equal(ctrl.count, 3)
  assert.equal(ctrl.waitingCount, 1)

  // 移除副岛 t2，t3 顺升为副岛，主岛 t1 不动
  mgr.remove('t2')
  assert.equal(ctrl.count, 2)
  assert.equal(ctrl.waitingCount, 0)
  assert.equal(ctrl.primaryId, 't1')
  assert.equal(ctrl.secondaryId, 't3')

  // 移除 t3，恢复单岛
  mgr.remove('t3')
  assert.equal(ctrl.count, 1)
  assert.equal(ctrl.hasSecondary, false)
  assert.equal(ctrl.primaryId, 't1')
  assert.equal(ctrl.secondaryId, null)

  // 移除 t1，恢复空闲
  mgr.remove('t1')
  assert.equal(ctrl.count, 0)
  assert.equal(ctrl.primaryId, null)
  assert.equal(ctrl.secondaryId, null)
})

test('IslandController：togglePin 与 toggleOverview 控制流验证', () => {
  const mgr = new ActivityManager()
  const ctrl = new IslandController(mgr)

  mgr.register({ id: 'task-1', source: 'a', title: 'Task 1' })
  mgr.register({ id: 'task-2', source: 'b', title: 'Task 2' })

  // 1. togglePin 锁定
  assert.equal(ctrl.pinnedId, null)
  ctrl.togglePin('task-2')
  assert.equal(ctrl.pinnedId, 'task-2')
  assert.equal(ctrl.primaryId, 'task-2')

  // 再次调用同一 ID 解锁
  ctrl.togglePin('task-2')
  assert.equal(ctrl.pinnedId, null)

  // 2. toggleOverview 开关
  assert.equal(ctrl.isOverviewOpen.value, false)
  ctrl.toggleOverview()
  assert.equal(ctrl.isOverviewOpen.value, true)
  assert.equal(ctrl.isExpanded.value, false)
  ctrl.toggleOverview()
  assert.equal(ctrl.isOverviewOpen.value, false)
})
