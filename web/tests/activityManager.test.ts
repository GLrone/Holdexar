import assert from 'node:assert/strict'
import { test } from 'node:test'

import { ActivityManager } from '../src/components/ui/activity/activityManager.ts'

test('ActivityManager：相同 ID 注册时原位更新，保留既有创建时间戳', () => {
  const mgr = new ActivityManager()

  const first = mgr.register({
    id: 'test-task',
    source: 'crawler',
    kind: 'task',
    status: 'running',
    title: '任务开始',
    progress: 10,
  })

  assert.ok(first)
  assert.equal(first.title, '任务开始')
  assert.equal(first.progress, 10)
  assert.equal(mgr.count, 1)
  const initialCreated = first.createdAt

  // 同 ID 再次更新进度
  const second = mgr.register({
    id: 'test-task',
    source: 'crawler',
    title: '任务推进中',
    progress: 50,
  })

  assert.ok(second)
  assert.equal(mgr.count, 1)
  assert.equal(second.title, '任务推进中')
  assert.equal(second.progress, 50)
  assert.equal(second.createdAt, initialCreated)
  assert.ok(second.updatedAt >= initialCreated)
})

test('ActivityManager：update 方法进行局部属性原位更新', () => {
  const mgr = new ActivityManager()
  mgr.register({
    id: 'sync-1',
    source: 'bills',
    kind: 'task',
    title: '同步账单',
    progress: null,
  })

  const updated = mgr.update('sync-1', {
    progress: 80,
    detail: '已导入 80 行',
  })

  assert.ok(updated)
  assert.equal(updated.progress, 80)
  assert.equal(updated.detail, '已导入 80 行')
  assert.equal(mgr.get('sync-1')?.progress, 80)
})

test('ActivityManager：完成与失败状态流转', () => {
  const mgr = new ActivityManager()
  mgr.register({
    id: 'download-1',
    source: 'updater',
    kind: 'task',
    title: '下载更新',
    status: 'running',
  })

  // 标记失败
  mgr.fail('download-1', {
    title: '下载失败',
    detail: '网络中断',
  })

  const failed = mgr.get('download-1')
  assert.ok(failed)
  assert.equal(failed.status, 'failed')
  assert.equal(failed.tone, 'error')
  assert.equal(failed.needsAttention, true)
  assert.equal(failed.title, '下载失败')

  // 恢复并标记成功
  mgr.complete('download-1', {
    title: '下载就绪',
    duration: 5000,
  })

  const done = mgr.get('download-1')
  assert.ok(done)
  assert.equal(done.status, 'success')
  assert.equal(done.needsAttention, false)
  assert.equal(done.duration, 5000)
})

test('ActivityManager：移除活动与订阅监听器触发', () => {
  const mgr = new ActivityManager()
  let notified = 0
  const unsub = mgr.subscribe(() => {
    notified += 1
  })

  mgr.register({
    id: 'n-1',
    source: 'notice',
    kind: 'notice',
    title: '提醒',
  })
  assert.equal(notified, 1)

  const removed = mgr.remove('n-1')
  assert.equal(removed, true)
  assert.equal(mgr.count, 0)
  assert.equal(notified, 2)

  unsub()
  mgr.register({ id: 'n-2', source: 'notice', title: '静音' })
  assert.equal(notified, 2)
})

test('ActivityManager：语义色彩静音拦截与批量清除', () => {
  const mgr = new ActivityManager()
  mgr.register({ id: 'act-info', source: 'notice', tone: 'info', title: '普通提示' })
  mgr.register({ id: 'act-warn', source: 'notice', tone: 'warning', title: '警告提示' })
  assert.equal(mgr.count, 2)

  // 静音 info
  mgr.muteTone('info')
  assert.equal(mgr.count, 1)
  assert.equal(mgr.get('act-info'), undefined)
  assert.ok(mgr.get('act-warn'))

  // 静音后新进相同语义项被丢弃
  const blocked = mgr.register({ id: 'act-info-2', source: 'notice', tone: 'info', title: '再次提示' })
  assert.equal(blocked, null)
  assert.equal(mgr.count, 1)
})

test('ActivityManager：默认 progress 为 undefined，在完成或失败时自动清除以防误显示进度条', () => {
  const mgr = new ActivityManager()

  // 1. 未指定 progress 时必须为 undefined，而非 null
  const notice = mgr.register({ id: 'test-notice', source: 'notice', title: '提示消息' })
  assert.equal(notice?.progress, undefined)

  // 2. 带进度的任务转为完成时，progress 自动清除为 undefined
  mgr.register({ id: 'test-prog', source: 'crawler', status: 'running', title: '抓取', progress: 50 })
  assert.equal(mgr.get('test-prog')?.progress, 50)

  mgr.complete('test-prog')
  assert.equal(mgr.get('test-prog')?.progress, undefined)

  // 3. 失败时同样清除进度
  mgr.register({ id: 'test-fail', source: 'updater', status: 'running', title: '下载', progress: 70 })
  mgr.fail('test-fail')
  assert.equal(mgr.get('test-fail')?.progress, undefined)
})
