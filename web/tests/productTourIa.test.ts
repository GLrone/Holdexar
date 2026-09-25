import assert from 'node:assert/strict'
import { test } from 'node:test'

import en from '../src/locales/en/productTour.ts'
import zh from '../src/locales/zh-CN/productTour.ts'

/* 导览 IA：导览词条只教用户主链——旧模型词（监控池 / 代理管理 /
   我页 / 工具箱 / 爬取 / 队列 / Worker / Queue / Proxy / Runtime / Clash /
   任务页）不得出现在首次导览用户可见文本中。扫描范围 = productTour 全部词条
   （即导览浮层渲染出的全部文本）；技术页面内部出现这些词不在本判据内。 */

const FORBIDDEN = [
  '监控池',
  '监控中心',
  '代理管理',
  '代理',
  'Clash',
  '商店',
  '我」页',
  '我页',
  '工具箱',
  '爬取',
  '队列',
  '导入监控',
  '任务页',
  'Watch pool',
  'Monitor Pool',
  'Proxy',
  'Runtime',
  'Queue',
  'Worker',
  'Crawl',
  'import & monitor',
]

test('导览词条不含旧模型词（首次用户可见文本 = productTour 全部 value）', () => {
  for (const [label, dict] of [
    ['zh', zh as Record<string, string>],
    ['en', en as Record<string, string>],
  ] as const) {
    for (const [key, value] of Object.entries(dict)) {
      assert.ok(typeof value === 'string' && value.length > 0, `${key} 缺词条`)
      for (const word of FORBIDDEN) {
        assert.ok(
          !value.includes(word),
          `${label} ${key} 命中导览禁词「${word}」：${value}`,
        )
      }
    }
  }
})

test('导览点名与导航词典逐字一致（找游戏 / 我的关注；绑定步带「可选」）', () => {
  const zhDict = zh as Record<string, string>
  // 各步标题就是导航名（定稿形态逐字出现）
  assert.ok(zhDict['productTour.stepFind.title']!.includes('找游戏'))
  assert.ok(zhDict['productTour.stepFollow.title']!.includes('我的关注'))
  // 账号绑定 = 可选增强：标题带「可选」，收尾重看入口指向「设置」页
  assert.match(zhDict['productTour.stepBind.title']!, /可选 · 绑定 Steam 账号/)
  assert.match(zhDict['productTour.done.p3']!, /「设置」页/)
  // 收尾主链句与教学步骤一一对应（提醒不单独设步，收尾一句话带过）
  assert.match(zhDict['productTour.done.emph']!, /找游戏 → 看价格 → 关注 → 自动更新/)
  assert.match(zhDict['productTour.done.p1']!, /价格提醒/)
})

test('数据来源讲清楚：自动获取 + 关注后周期更新，两处口径一致', () => {
  const zhDict = zh as Record<string, string>
  assert.match(zhDict['productTour.stepPrice.p1']!, /自动从 Steam 全球 40 多个区获取/)
  assert.match(zhDict['productTour.stepPrice.emph']!, /关注后每 6 小时自动更新一轮/)
  assert.match(zhDict['productTour.done.p1']!, /每 6 小时自动更新/)
})

test('添加 ≠ 关注：导览必须显式分开两个概念，且不得把账号写成前置条件', () => {
  const zhDict = zh as Record<string, string>
  assert.match(zhDict['productTour.stepPrice.emph']!, /添加游戏 ≠ 关注/)
  assert.match(zhDict['productTour.stepFollow.emph']!, /关注后，系统会在后台持续更新价格/)
  // 账号只以「可选 / 不需要」的形态出现
  assert.match(zhDict['productTour.stepPrice.p2']!, /不需要绑定 Steam 账号/)
  assert.match(zhDict['productTour.stepBind.emph']!, /不绑定也完全不影响/)
  // 不得出现「需要绑定 / 必须绑定 / 先去…绑定」类前置表述（「不需要绑定」除外）
  for (const value of Object.values(zhDict)) {
    assert.ok(
      !/(?<!不)需要绑定 Steam|必须绑定|先去[^。]*绑定/.test(value),
      `导览出现账号前置表述：${value}`,
    )
  }
})
