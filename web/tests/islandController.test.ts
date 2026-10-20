import assert from 'node:assert/strict'
import { test } from 'node:test'

import { ActivityManager } from '../src/components/ui/activity/activityManager.ts'
import { IslandController } from '../src/components/ui/activity/islandController.ts'

function setup(ids: string[]) {
  const manager = new ActivityManager()
  for (const id of ids) {
    manager.register({ id, source: 'test', kind: 'task', title: id, priority: 30 })
  }
  return { manager, controller: new IslandController(manager) }
}

function assertNoDuplicateSlots(controller: IslandController) {
  const visibleIds = [controller.primaryId, controller.secondaryId, controller.expandedId]
    .filter((id): id is string => Boolean(id && !id.startsWith('@')))
  assert.equal(new Set(visibleIds).size, visibleIds.length)
  const allIds = [...visibleIds, ...controller.layout.waitingIds]
  assert.equal(new Set(allIds).size, allIds.length)
  assert.equal(allIds.length, controller.count)
}

test('IslandController: urgent activity preempts main while keeping other slots unique', () => {
  const { manager, controller } = setup(['crawl-task', 'secondary-task'])
  controller.openSecondary()
  assert.equal(controller.primaryId, 'crawl-task')
  assert.equal(controller.secondaryId, null)
  assert.equal(controller.expandedId, 'secondary-task')

  manager.register({
    id: 'proxy-error',
    source: 'proxypool',
    kind: 'notice',
    status: 'failed',
    needsAttention: true,
    title: 'Proxy unavailable',
  })

  assert.equal(controller.primaryId, 'proxy-error')
  assert.equal(controller.expandedId, 'secondary-task')
  assertNoDuplicateSlots(controller)
})

test('IslandController: progress refresh preserves the user-selected layout', () => {
  const { manager, controller } = setup(['updater-task', 'crawl-task', 'backup-task'])
  controller.selectPrimary('updater-task')
  controller.openSecondary()
  const before = controller.layout

  manager.update('crawl-task', { progress: 20, title: 'Crawling 20%', priority: 99 })
  manager.update('backup-task', { progress: 40, title: 'Backup 40%' })

  assert.deepEqual(controller.layout, before)
  assert.equal(controller.expandedActivity?.progress, 20)
})

test('IslandController: secondary opens directly and fills from waiting candidates', () => {
  const { controller } = setup(['A', 'B', 'C', 'D'])
  controller.openSecondary()

  assert.equal(controller.primaryId, 'A')
  assert.equal(controller.secondaryId, 'C')
  assert.equal(controller.expandedId, 'B')
  assert.deepEqual(controller.waitingIds, ['D'])
  assertNoDuplicateSlots(controller)
})

test('IslandController: two activities open secondary without promoting it to main', () => {
  const { controller } = setup(['A', 'B'])
  controller.openSecondary()

  assert.equal(controller.primaryId, 'A')
  assert.equal(controller.secondaryId, null)
  assert.equal(controller.expandedId, 'B')
  assert.equal(controller.waitingCount, 0)
})

test('IslandController: secondary and expanded tasks exchange without consuming the queue', () => {
  const { controller } = setup(['A', 'B', 'C', 'D'])
  controller.openSecondary()
  controller.openSecondary()

  assert.deepEqual(controller.layout, {
    mainId: 'A',
    secondaryId: 'B',
    expandedId: 'C',
    waitingIds: ['D'],
  })
  assertNoDuplicateSlots(controller)
})

test('IslandController: main click while expanded swaps only main and expanded', () => {
  const { controller } = setup(['A', 'B', 'C', 'D'])
  controller.openSecondary()
  controller.openMain()

  assert.deepEqual(controller.layout, {
    mainId: 'B',
    secondaryId: 'C',
    expandedId: 'A',
    waitingIds: ['D'],
  })
})

test('IslandController: compact main opens to expanded and refills secondary from candidates', () => {
  const { controller } = setup(['A', 'B', 'C', 'D'])
  controller.openMain()

  assert.deepEqual(controller.layout, {
    mainId: 'B',
    secondaryId: 'C',
    expandedId: 'A',
    waitingIds: ['D'],
  })
  assertNoDuplicateSlots(controller)
})

test('IslandController: a single activity expands while the compact anchor remains available', () => {
  const { controller } = setup(['A'])
  controller.openMain()

  assert.equal(controller.isExpanded.value, true)
  assert.equal(controller.primaryId, null)
  assert.equal(controller.expandedId, 'A')
  assert.equal(controller.count, 1)
})

test('IslandController: return restores only the previous layout snapshot', () => {
  const { controller } = setup(['A', 'B', 'C', 'D'])
  const initial = controller.layout
  controller.openSecondary()
  controller.openMain()
  assert.equal(controller.primaryId, 'B')

  controller.returnPreviousLayout()
  assert.deepEqual(controller.layout, {
    mainId: 'A',
    secondaryId: 'C',
    expandedId: 'B',
    waitingIds: ['D'],
  })

  controller.returnPreviousLayout()
  assert.deepEqual(controller.layout, initial)
})

test('IslandController: collapse returns the expanded task to its origin slot', () => {
  const { controller } = setup(['A', 'B', 'C', 'D'])
  controller.openSecondary()
  controller.collapseExpanded()
  assert.deepEqual(controller.layout, {
    mainId: 'A',
    secondaryId: 'B',
    expandedId: null,
    waitingIds: ['C', 'D'],
  })

  controller.openMain()
  controller.collapseExpanded()
  assert.deepEqual(controller.layout, {
    mainId: 'A',
    secondaryId: 'B',
    expandedId: null,
    waitingIds: ['C', 'D'],
  })
})

test('IslandController: removed and expired activities leave no stale slot or duplicate', async () => {
  const manager = new ActivityManager()
  const controller = new IslandController(manager)
  manager.register({ id: 'A', source: 'test', title: 'A' })
  manager.register({ id: 'B', source: 'test', title: 'B', duration: 10 })
  manager.register({ id: 'C', source: 'test', title: 'C' })
  controller.openSecondary()

  await new Promise((resolve) => setTimeout(resolve, 30))
  assert.equal(controller.primaryId, 'A')
  assert.equal(controller.expandedId, null)
  assert.equal(controller.secondaryId, 'C')
  assertNoDuplicateSlots(controller)

  manager.remove('A')
  assert.equal(controller.primaryId, 'C')
  assert.equal(controller.secondaryId, null)
  assertNoDuplicateSlots(controller)
})

test('IslandController: pin and urgent priority semantics remain intact', () => {
  const { manager, controller } = setup(['A', 'B', 'C'])
  controller.pin('B')
  assert.equal(controller.primaryId, 'B')

  manager.register({
    id: 'urgent',
    source: 'system',
    kind: 'system',
    status: 'failed',
    needsAttention: true,
    title: 'Urgent',
  })
  assert.equal(controller.primaryId, 'urgent')
  assert.equal(controller.pinnedId, 'B')

  manager.remove('urgent')
  assert.equal(controller.primaryId, 'B')
  controller.unpin()
  assert.equal(controller.pinnedId, null)
  assertNoDuplicateSlots(controller)
})

test('IslandController: repeated transitions do not duplicate or lose activities', () => {
  const { controller } = setup(['A', 'B', 'C', 'D', 'E', 'F'])
  for (let index = 0; index < 25; index += 1) {
    controller.openSecondary()
    controller.openMain()
    if (index % 2 === 0) controller.returnPreviousLayout()
    assertNoDuplicateSlots(controller)
    assert.equal(controller.count, 6)
    assert.equal(controller.layout.waitingIds.length + [controller.primaryId, controller.secondaryId, controller.expandedId].filter((id) => id && !id.startsWith('@')).length, 6)
  }
})

test('IslandController: pin, overview, and compatibility expansion APIs remain connected', () => {
  const { controller } = setup(['A', 'B'])
  controller.togglePin('B')
  assert.equal(controller.pinnedId, 'B')
  assert.equal(controller.primaryId, 'B')
  controller.togglePin('B')
  assert.equal(controller.pinnedId, null)

  controller.setExpanded(true)
  assert.equal(controller.isExpanded.value, true)
  controller.toggleOverview()
  assert.equal(controller.isOverviewOpen.value, true)
  assert.equal(controller.isExpanded.value, false)
  controller.setOverview(false)
  controller.setExpanded(false)
  assert.equal(controller.isExpanded.value, false)
})
