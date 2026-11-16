import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { afterEach, beforeEach, test } from 'node:test'

import { activityManager } from '../src/components/ui/activity/activityManager.ts'
import { islandController } from '../src/components/ui/activity/islandController.ts'
import { beginTask, dropNotice, endTask, pushNotice, updateNotice, updateTask } from '../src/components/ui/island.ts'

beforeEach(() => {
  activityManager.clear()
  islandController.setHovered(false)
  islandController.collapseExpanded()
  islandController.setOverview(false)
})
afterEach(() => activityManager.clear())

const notice = { tone: 'info' as const, text: 'Notice', detail: '', sticky: false,
  duration: 0, retry: null, to: '', toLabel: '' }

for (const [tone, interaction] of (['success', 'warning', 'info'] as const)
  .flatMap(tone => (['hover', 'expanded'] as const).map(interaction => [tone, interaction] as const))) {
  test(`notice lifetime: light ${tone} expires on schedule even under ${interaction}`, (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] })
    const id = pushNotice({ ...notice, tone, duration: 2400 })
    t.mock.timers.tick(1000)
    if (interaction === 'hover') islandController.setHovered(true)
    else islandController.openMain()
    t.mock.timers.tick(3000)
    assert.equal(activityManager.get(`notice:${id}`), undefined)
    islandController.setHovered(false)
    islandController.collapseExpanded()
    assert.equal(activityManager.count, 0)
    assert.equal(islandController.primaryActivity, null)
    assert.equal(islandController.expandedActivity, null)
  })
}

for (const interaction of ['hover', 'expanded'] as const) {
  test(`notice lifetime: error survives ${interaction} and expires after release`, (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] })
    const id = pushNotice({ ...notice, tone: 'error', duration: 2400 })
    t.mock.timers.tick(1000)
    if (interaction === 'hover') islandController.setHovered(true)
    else islandController.openMain()
    t.mock.timers.tick(3000)
    assert.ok(activityManager.get(`notice:${id}`))
    islandController.setHovered(false)
    islandController.collapseExpanded()
    t.mock.timers.tick(2399)
    assert.ok(activityManager.get(`notice:${id}`))
    t.mock.timers.tick(1)
    assert.equal(activityManager.count, 0)
    assert.equal(islandController.primaryActivity, null)
    assert.equal(islandController.expandedActivity, null)
  })
}

test('notice adapter: any removal is final and updates cannot resurrect it', () => {
  const id = pushNotice({ ...notice, text: 'Saved', iconImageUrl: 'https://example.test/cover.jpg' })
  assert.equal(activityManager.get(`notice:${id}`)?.iconImageUrl, 'https://example.test/cover.jpg')
  updateNotice(id, 'Updated')
  assert.equal(islandController.primaryActivity?.title, 'Updated')
  activityManager.remove(`notice:${id}`)
  updateNotice(id, 'Late update')
  dropNotice(id)
  assert.equal(activityManager.count, 0)
  assert.equal(islandController.primaryActivity, null)
})

test('notice adapter: replacement and capacity use the same activity records', () => {
  beginTask('bills-sync', 'Syncing')
  const ids = Array.from({ length: 6 }, () => pushNotice(notice))
  assert.deepEqual(activityManager.activities.filter(a => a.source === 'notice').map(a => a.id),
    ids.slice(-4).map(id => `notice:${id}`))
  assert.ok(activityManager.get('task:bills-sync'))
  const loading = pushNotice({ ...notice, sticky: true, tone: 'progress' })
  pushNotice({ ...notice, text: 'Completed' })
  assert.equal(activityManager.get(`notice:${loading}`), undefined)
  assert.equal(activityManager.activities.filter(a => a.source === 'notice').length, 4)
})

test('notice retry retires the old record before publishing a replacement', () => {
  let calls = 0
  const id = pushNotice({ ...notice, tone: 'error', retry: () => {
    calls++
    assert.equal(activityManager.count, 0)
    pushNotice({ ...notice, text: 'Recovered' })
  } })
  islandController.openMain()
  activityManager.retry(`notice:${id}`)
  assert.equal(calls, 1)
  assert.equal(activityManager.get(`notice:${id}`), undefined)
  assert.equal(activityManager.count, 1)
  assert.equal(islandController.primaryActivity?.title, 'Recovered')
})

test('task adapter: update, repeat registration and removal share one record', () => {
  beginTask('bills-sync', 'Syncing', { percent: 10, to: '/bills' })
  beginTask('bills-sync', 'Continued', { percent: 20, to: '/bills' })
  assert.equal(activityManager.count, 1)
  islandController.openMain()
  updateTask('bills-sync', 'Halfway', 50)
  assert.equal(islandController.expandedActivity?.title, 'Halfway')
  assert.equal(islandController.expandedActivity?.progress, 50)
  assert.equal(islandController.expandedActivity?.to, '/bills')
  activityManager.remove('task:bills-sync')
  updateTask('bills-sync', 'Late update', 80)
  endTask('bills-sync')
  assert.equal(activityManager.count, 0)
  assert.equal(islandController.expandedActivity, null)
})

test('island architecture: the view has no secondary store or source-specific cleanup', () => {
  const source = readFileSync(new URL('../src/components/ui/HlIsland.vue', import.meta.url), 'utf8')
  const adapter = readFileSync(new URL('../src/components/ui/island.ts', import.meta.url), 'utf8')
  assert.doesNotMatch(source, /legacyNotice|legacyTask|dropNotice|endTask|armTimer|notice:|task:/)
  assert.doesNotMatch(adapter, /reactive\(|setTimeout|clearTimeout|export const island\b/)
  assert.match(source, /activityManager\.retry\(act\.id\)/)
})
