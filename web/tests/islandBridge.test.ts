import assert from 'node:assert/strict'
import { test } from 'node:test'

import { activityManager } from '../src/components/ui/activity/activityManager.ts'
import {
  beginTask,
  dropNotice,
  endTask,
  island,
  pushNotice,
  updateNotice,
  updateTask,
} from '../src/components/ui/island.ts'

test('islandBridge：beginTask 与 updateTask/endTask 正确桥接至 ActivityManager', () => {
  activityManager.clear()
  island.tasks = []

  beginTask('bills-sync', '账单同步中', { percent: 10, to: '/bills' })

  // 既有 island.tasks 数组同步存有该项
  assert.equal(island.tasks.length, 1)
  assert.equal(island.tasks[0].key, 'bills-sync')
  assert.equal(island.tasks[0].text, '账单同步中')

  // ActivityManager 存在对应活动
  const act = activityManager.get('task:bills-sync')
  assert.ok(act)
  assert.equal(act.title, '账单同步中')
  assert.equal(act.progress, 10)
  assert.equal(act.source, 'bills')

  // updateTask 原位更新
  updateTask('bills-sync', '已导入 50 行', 50)
  assert.equal(island.tasks[0].text, '已导入 50 行')
  assert.equal(island.tasks[0].percent, 50)
  assert.equal(activityManager.get('task:bills-sync')?.title, '已导入 50 行')
  assert.equal(activityManager.get('task:bills-sync')?.progress, 50)

  // endTask 撤下
  endTask('bills-sync')
  assert.equal(island.tasks.length, 0)
  assert.equal(activityManager.get('task:bills-sync'), undefined)
})

test('islandBridge：pushNotice 与 dropNotice/updateNotice 正确桥接至 ActivityManager', () => {
  activityManager.clear()
  island.notices = []

  const id = pushNotice({
    tone: 'success',
    text: '设置已保存',
    detail: '配置已生效',
    sticky: false,
    duration: 3000,
    retry: null,
    to: '',
    toLabel: '',
    iconImageUrl: 'https://example.test/game-cover.jpg',
  })

  assert.ok(id > 0)
  assert.equal(island.notices.length, 1)
  assert.equal(island.notices[0].text, '设置已保存')

  // ActivityManager 中存在映射项
  const act = activityManager.get(`notice:${id}`)
  assert.ok(act)
  assert.equal(act.title, '设置已保存')
  assert.equal(act.detail, '配置已生效')
  assert.equal(act.tone, 'success')
  assert.equal(act.iconImageUrl, 'https://example.test/game-cover.jpg')

  // updateNotice 原位更新
  updateNotice(id, '设置已同步至云端')
  assert.equal(island.notices[0].text, '设置已同步至云端')
  assert.equal(activityManager.get(`notice:${id}`)?.title, '设置已同步至云端')

  // dropNotice
  dropNotice(id)
  assert.equal(island.notices.length, 0)
  assert.equal(activityManager.get(`notice:${id}`), undefined)
})
