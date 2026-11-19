import assert from 'node:assert/strict'
import { test } from 'node:test'

import { ActivityManager } from '../src/components/ui/activity/activityManager.ts'
import { resolveActivityCardView } from '../src/components/ui/activity/presentationResolver.ts'
import type { Activity } from '../src/components/ui/activity/types.ts'

function activity(patch: Partial<Activity> = {}): Activity {
  return {
    id: 'task:demo',
    source: 'demo',
    kind: 'task',
    status: 'running',
    title: 'Demo',
    priority: 30,
    createdAt: 0,
    updatedAt: 0,
    ...patch,
  }
}

test('card view：自带取消的运行任务不再重复提供忽略', () => {
  const view = resolveActivityCardView(activity({ cancel: () => {} }))
  assert.equal(view.cancel !== null, true)
  assert.equal(view.cancelable, true)
  assert.equal(view.dismissible, false)
  assert.equal(view.busy, true)

  const ended = resolveActivityCardView(activity({ cancel: () => {}, status: 'failed' }))
  assert.equal(ended.cancelable, false)
  assert.equal(ended.dismissible, true)
})

test('card view：普通运行任务保留忽略，失败动作落到日志页', () => {
  const running = resolveActivityCardView(activity())
  assert.equal(running.cancel, null)
  assert.equal(running.dismissible, true)

  const failed = resolveActivityCardView(activity({ status: 'failed', retry: () => {} }))
  assert.equal(failed.target, '/logs')
  assert.equal(failed.targetLabelKind, 'logs')
  assert.equal(failed.canRetry, true)
  assert.equal(failed.dismissible, true)
})

test('card view：空闲价格态主动作直达任务中心，跳转词条按目的地归类', () => {
  const idle = resolveActivityCardView(null, { showingPrice: true })
  assert.equal(idle.target, '/crawl')
  assert.equal(idle.targetLabelKind, 'task')

  assert.equal(resolveActivityCardView(activity({ to: '/bills' })).targetLabelKind, 'view')
  assert.equal(resolveActivityCardView(activity({ to: '/crawl' })).targetLabelKind, 'task')
})

test('card view：进度呈现仅限运行中任务', () => {
  assert.equal(resolveActivityCardView(activity()).busy, true)
  assert.equal(resolveActivityCardView(activity({ progress: 40 })).busy, true)
  assert.equal(resolveActivityCardView(activity({ kind: 'notice', progress: undefined })).busy, false)
  assert.equal(resolveActivityCardView(activity({ status: 'success', progress: 100 })).busy, false)
})

test('activity manager：cancel 与 retry 一样随注册合并、可被更新替换', () => {
  const manager = new ActivityManager()
  const first = () => {}
  manager.register({ id: 'task:demo', source: 'demo', title: 'Demo', cancel: first })
  assert.equal(manager.get('task:demo')?.cancel, first)

  const second = () => {}
  manager.update('task:demo', { cancel: second })
  assert.equal(manager.get('task:demo')?.cancel, second)

  manager.update('task:demo', { status: 'success' })
  assert.equal(manager.get('task:demo')?.cancel, second)
})
