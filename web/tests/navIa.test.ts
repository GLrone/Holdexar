import assert from 'node:assert/strict'
import { test } from 'node:test'

import en from '../src/locales/en/shell.ts'
import zh from '../src/locales/zh-CN/shell.ts'

/* P-M3 导航 IA：核心四入口的职责词、三组结构、一级导航禁词。
   只钉用户面词条 value——route / store / API 名不在这里约束。 */

test('四个核心心智入口的命名固定（找游戏=发现 / 游戏库=拥有 / 我的关注=持续关注 / 价格提醒=主动通知）', () => {
  const coreZh: Record<string, string> = {
    'nav.library': '找游戏',
    'nav.gamelib': '游戏库',
    'nav.pool': '我的关注',
    'nav.alerts': '价格提醒',
  }
  for (const [key, value] of Object.entries(coreZh)) {
    assert.equal((zh as Record<string, string>)[key], value, `${key} zh 命名漂移`)
  }
  const coreEn: Record<string, string> = {
    'nav.library': 'Find Games',
    'nav.gamelib': 'Game Library',
    'nav.pool': 'Following',
    'nav.alerts': 'Price Alerts',
  }
  for (const [key, value] of Object.entries(coreEn)) {
    assert.equal((en as Record<string, string>)[key], value, `${key} en 命名漂移`)
  }
})

test('系统组命名固定（网络 / 任务 / 汇率 / 日志 / 设置 / 关于），任务本轮只定归属不改名', () => {
  const systemZh: Record<string, string> = {
    'nav.proxies': '网络',
    'nav.crawl': '任务',
    'nav.rates': '汇率',
    'nav.logs': '日志',
    'nav.me': '设置',
    'nav.about': '关于',
  }
  for (const [key, value] of Object.entries(systemZh)) {
    assert.equal((zh as Record<string, string>)[key], value, `${key} zh 命名漂移`)
  }
  assert.equal((en as Record<string, string>)['nav.proxies'], 'Network')
  assert.equal((en as Record<string, string>)['nav.me'], 'Settings')
})

test('更多组命名固定（捆绑包 / 家庭 / 账单 / 成就）', () => {
  const moreZh: Record<string, string> = {
    'nav.bundles': '捆绑包',
    'nav.family': '家庭',
    'nav.bills': '账单',
    'nav.achievements': '成就',
  }
  for (const [key, value] of Object.entries(moreZh)) {
    assert.equal((zh as Record<string, string>)[key], value, `${key} zh 命名漂移`)
  }
})

test('一级导航词条不含实现词（监控池 / 代理 / 爬取 / 节点 / 工具箱 / 队列 / Watch pool / Proxies / Toolbox）', () => {
  // 侧栏实际成员（与 App.vue navGroups 同构）：核心组 + 更多 + 系统
  const sidebarKeys = [
    'nav.dashboard',
    'nav.library',
    'nav.gamelib',
    'nav.pool',
    'nav.alerts',
    'nav.bundles',
    'nav.family',
    'nav.bills',
    'nav.achievements',
    'nav.proxies',
    'nav.crawl',
    'nav.rates',
    'nav.logs',
    'nav.me',
    'nav.about',
  ]
  const forbidden = [
    '监控池',
    '监控中心',
    '代理管理',
    '代理',
    '爬取',
    '队列',
    '节点',
    '工具箱',
    'Watch pool',
    'Monitor Pool',
    'Proxies',
    'Proxy',
    'Toolbox',
    'Queue',
    'Worker',
    'Crawl',
  ]
  for (const dict of [zh, en] as Record<string, string>[]) {
    for (const key of sidebarKeys) {
      const value = dict[key]
      assert.ok(typeof value === 'string' && value.length > 0, `${key} 缺词条`)
      for (const word of forbidden) {
        assert.ok(!value.includes(word), `${key} 命中一级导航禁词「${word}」：${value}`)
      }
    }
  }
  // 「任务」本轮允许保留（只解决归属，不解决命名）
  assert.equal((zh as Record<string, string>)['nav.crawl'], '任务')
  // 工具箱不在侧栏任何组里：词条只为页面标题与设置页次级入口而留
  assert.ok(!sidebarKeys.includes('nav.toolbox'))
  assert.equal((zh as Record<string, string>)['nav.toolbox'], '工具箱')
})

test('分组词条只有 更多 / 系统 两组（核心组不带组名）', () => {
  const groupKeys = Object.keys(zh as Record<string, string>).filter((k) =>
    k.startsWith('nav.group.'),
  )
  assert.deepEqual(
    groupKeys.sort(),
    ['nav.group.more', 'nav.group.system'],
    '分组 key 集合漂移',
  )
  assert.equal((zh as Record<string, string>)['nav.group.more'], '更多')
  assert.equal((zh as Record<string, string>)['nav.group.system'], '系统')
  assert.equal((en as Record<string, string>)['nav.group.more'], 'More')
  assert.equal((en as Record<string, string>)['nav.group.system'], 'System')
})
