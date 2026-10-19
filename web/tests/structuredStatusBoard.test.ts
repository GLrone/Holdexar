import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

const proxiesPageSource = readFileSync(new URL('../src/views/proxies/Index.vue', import.meta.url), 'utf8')
const statusBoardSource = readFileSync(new URL('../src/components/structured/components/StatusBoard.vue', import.meta.url), 'utf8')
const blockStart = proxiesPageSource.indexOf('const proxyStatusBlock = computed')
const blockEnd = proxiesPageSource.indexOf('\nasync function load()', blockStart)
const proxyBlockSource = proxiesPageSource.slice(blockStart, blockEnd)

test('代理状态页按状态看板分组契约传递节点状态', () => {
  assert.ok(proxyBlockSource.includes('groups: ['), '代理状态数据必须使用状态看板分组结构')
  assert.ok(proxyBlockSource.includes('items: boardItems'), '代理状态条目必须进入分组')
  assert.ok(proxyBlockSource.includes("version: '1.0'"), '结构化块必须标明协议版本')
  assert.ok(statusBoardSource.includes('props.data?.groups'), '状态看板必须读取分组结构')
})
