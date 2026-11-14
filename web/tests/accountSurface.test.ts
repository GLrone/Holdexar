import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import { createPinia, defineStore, setActivePinia } from 'pinia'
import { computed, ref } from 'vue'
import ts from 'typescript'
import { parse } from '@vue/compiler-sfc'

import zhSettings from '../src/locales/zh-CN/settings.ts'
import enSettings from '../src/locales/en/settings.ts'
import zhShell from '../src/locales/zh-CN/shell.ts'
import enShell from '../src/locales/en/shell.ts'

/* ── helpers：SFC 源码抽取 + TS 转译执行（与 accountDataRefresh.test.ts 同法）── */

function sfc(url: string) {
  const source = readFileSync(new URL(url, import.meta.url), 'utf8')
  const d = parse(source).descriptor
  return { tpl: d.template!.content, script: d.scriptSetup!.content }
}

function pick(src: string, pred: (s: ts.Statement, text: string) => boolean): string {
  const ast = ts.createSourceFile('s.ts', src, ts.ScriptTarget.Latest, true)
  const stmt = ast.statements.find((s) => pred(s, s.getText(ast)))
  assert.ok(stmt, '未在源码中找到目标语句')
  return ts.transpileModule(stmt.getText(ast), { compilerOptions: { target: ts.ScriptTarget.Latest } }).outputText
}

function fnOf(src: string, name: string): string {
  return pick(src, (s) => ts.isFunctionDeclaration(s) && s.name?.text === name)
}

function run(js: string, ret: string, bindings: Record<string, unknown>) {
  return new Function(...Object.keys(bindings), `${js}\nreturn ${ret}`)(...Object.values(bindings))
}

/* 6 账号 mock 快照（与 AccountStatus 契约同形） */
function statusOf(active: string, bump: Record<string, number> = {}) {
  return {
    has_cookie: true, cookie_steam_id: 'S1', bound_steam_id: 'S1', mismatch: false,
    profile: null, wallet: null,
    accounts: ['S1', 'S2', 'S3', 'S4', 'S5', 'S6'].map((sid, i) => ({
      steam_id: sid, friend_code: `F${i}`, persona_name: `P${i}`, avatar_url: '',
      is_active: sid === active, is_primary: i === 0, bound_at: null,
      wallet: {
        balance: i + 1 + (bump[sid] ?? 0), balance_display: `¥${i + 1 + (bump[sid] ?? 0)}.00`,
        currency_code: 'CNY', currency_symbol: '¥', currency_id: 23, region_code: 'CN',
        country_code: 'CN', checked_at: null, ok_at: null, check_ok: true, error: '',
      },
      wallet_error: '', session_expired: false, session_expires_at: null, session_has_refresh: false,
      wishlist_count: i * 3, game_count: 100 + i + (bump[sid] ?? 0), bills_synced_at: null,
      redeem_used: 0, is_online: false, in_game: '',
    })),
    primary_steam_id: 'S1', session_expired: false, session_expires_at: null,
    session_has_refresh: false, is_online: false, in_game: '',
  }
}

/* ── i18n：新词条双语齐备、占位符一致 ── */

const SHELL_KEYS = ['wallet.popSwitchTip', 'wallet.toastSwitched'] as const
const SETTINGS_KEYS = [
  'settings.steam.syncOne', 'settings.steam.accountCount',
  'settings.toast.accountSyncQueued', 'settings.toast.accountSyncFailed',
] as const

test('账号切换/同步新词条：中英双语齐备', () => {
  for (const [zh, en, keys] of [
    [zhShell as Record<string, string>, enShell as Record<string, string>, SHELL_KEYS],
    [zhSettings as Record<string, string>, enSettings as Record<string, string>, SETTINGS_KEYS],
  ] as const) {
    for (const key of keys) {
      assert.ok(typeof zh[key] === 'string' && zh[key].length > 0, `zh 缺 ${key}`)
      assert.ok(typeof en[key] === 'string' && en[key].length > 0, `en 缺 ${key}`)
    }
  }
})

test('账号切换/同步新词条：中英占位符逐条一致', () => {
  const holes = (s: string) => (s.match(/\{\w+\}/g) ?? []).sort().join(',')
  for (const [zh, en, keys] of [
    [zhShell as Record<string, string>, enShell as Record<string, string>, SHELL_KEYS],
    [zhSettings as Record<string, string>, enSettings as Record<string, string>, SETTINGS_KEYS],
  ] as const) {
    for (const key of keys) {
      assert.equal(holes(zh[key]!), holes(en[key]!), `${key} 占位符不一致`)
    }
  }
})

/* ── 顶栏余额弹层：行内头像/昵称为点击区，余额区不挂点击 ── */

test('余额弹层账号行：头像+昵称包在切换按钮内，余额在按钮外', () => {
  const { tpl } = sfc('../src/App.vue')
  const rowStart = tpl.indexOf('class="wallet-balances-row"')
  const rowEnd = tpl.indexOf('wallet-balances-pop__foot', rowStart)
  const row = tpl.slice(rowStart, rowEnd)
  const idBtn = row.indexOf('wallet-balances-row__id')
  const avatar = row.indexOf('wallet-balances-row__avatar')
  const name = row.indexOf('wallet-balances-row__name')
  const btnClose = row.indexOf('</button>', name)
  const balance = row.indexOf('wallet-balances-row__balance')
  assert.ok(idBtn >= 0, '缺少切换点击区元素')
  assert.ok(avatar > idBtn && name > avatar, '头像/昵称应在点击区内')
  assert.ok(btnClose > name && balance > btnClose, '余额区必须在按钮之外（点空白不误切）')
  assert.ok(row.includes('@click="switchWalletAccount(acc)"'), '点击区未挂切换处理')
})

test('switchWalletAccount：非当前行调 setActive 并弹成功提示，当前行/切换中不动', async () => {
  const calls: string[] = []
  const toasts: string[] = []
  let switching = false
  const accountStore = {
    get switching() { return switching },
    async setActive(sid: string) { calls.push(sid); return statusOf(sid) },
  }
  const t = (k: string, p?: Record<string, unknown>) => `${k}:${JSON.stringify(p ?? {})}`
  const message = { success: (s: string) => toasts.push(s), error: (s: string) => toasts.push(s) }
  const js = fnOf(sfc('../src/App.vue').script, 'switchWalletAccount')
  const runOne = (acc: { steam_id: string; is_active: boolean; persona_name: string }) =>
    run(js, 'switchWalletAccount', { accountStore, message, t })({ steam_id: acc.steam_id, is_active: acc.is_active, persona_name: acc.persona_name })

  await runOne({ steam_id: 'S3', is_active: false, persona_name: 'P3' })
  assert.deepEqual(calls, ['S3'])
  assert.ok(toasts[0]!.startsWith('wallet.toastSwitched'), '缺成功提示')

  await runOne({ steam_id: 'S1', is_active: true, persona_name: 'P1' })
  assert.deepEqual(calls, ['S3'], '当前账号行不得重复触发切换')

  switching = true
  await runOne({ steam_id: 'S4', is_active: false, persona_name: 'P4' })
  assert.deepEqual(calls, ['S3'], '切换进行中应被挡住')
})

test('switchWalletAccount：失败走错误提示不抛出', async () => {
  const errors: string[] = []
  const accountStore = {
    switching: false,
    async setActive() { throw new Error('boom') },
  }
  const js = fnOf(sfc('../src/App.vue').script, 'switchWalletAccount')
  await run(js, 'switchWalletAccount', {
    accountStore,
    message: { success: () => assert.fail('不应成功'), error: (s: string) => errors.push(s) },
    t: (k: string) => k,
  })({ steam_id: 'S2', is_active: false, persona_name: 'P2' })
  assert.equal(errors.length, 1)
  assert.ok(errors[0]!.includes('boom'))
})

/* ── 设置页：行内「同步」按钮 + 行为 ── */

test('设置页账号行：同步按钮在操作区且排在「设为当前/解绑」之前', () => {
  const { tpl } = sfc('../src/views/settings/Index.vue')
  const listStart = tpl.indexOf('class="account-list"')
  const listEnd = tpl.indexOf('account-summary__meta', listStart)
  const list = tpl.slice(listStart, listEnd)
  const actions = list.indexOf('account-row__actions')
  const syncOne = list.indexOf("t('settings.steam.syncOne')")
  const setActive = list.indexOf("t('settings.steam.setActive')")
  const unbind = list.indexOf("t('settings.steam.unbind')")
  assert.ok(actions >= 0 && syncOne > actions, '同步按钮应在行操作区')
  assert.ok(syncOne < setActive && setActive < unbind, '行操作顺序应为 同步 → 设为当前 → 解绑')
})

test('账号卡标题：N 个账号计数绑定 accounts.length', () => {
  const { tpl } = sfc('../src/views/settings/Index.vue')
  const headStart = tpl.indexOf('settings.section.steamAccount')
  const listStart = tpl.indexOf('class="account-list"')
  assert.ok(headStart >= 0 && headStart < listStart)
  const head = tpl.slice(headStart, listStart)
  assert.ok(head.includes("t('settings.steam.accountCount', { n: accounts.length })"), '标题缺账号计数')
})

test('refreshOneAccount：ok 后弹「已触发同步」并调度延迟快照重拉，失败弹错误，进行中挡并发', async () => {
  const script = sfc('../src/views/settings/Index.vue').script
  const ast = ts.createSourceFile('s.ts', script, ts.ScriptTarget.Latest, true)
  const varJs = ast.statements
    .filter((s) => ts.isVariableStatement(s) &&
      (s.getText(ast).includes('syncingSteamId') || s.getText(ast).includes('snapshotRefreshTimer')))
    .map((s) => ts.transpileModule(s.getText(ast), { compilerOptions: { target: ts.ScriptTarget.Latest } }).outputText)
    .join('\n')
  const js =
    varJs + '\n' +
    fnOf(script, 'scheduleSnapshotRefresh') + '\n' +
    fnOf(script, 'refreshOneAccount')
  const calls: string[] = []
  const toasts: string[] = []
  let loadCalls = 0
  const accountStore = {
    async refreshAccount(sid: string) { calls.push(sid); return { ok: true, steamId: sid, wallet: { ok: true, wallet: {} }, queued: ['wishlist', 'family', 'bills'] } },
    load() { loadCalls++; return Promise.resolve() },
  }
  const t = (k: string, p?: Record<string, unknown>) => `${k}:${JSON.stringify(p ?? {})}`
  const message = { success: (s: string) => toasts.push(s), error: (s: string) => toasts.push(s) }

  /* 捕获定时器：验收「ok 后 4s 调 load」与「重复点击只留最后一拍」 */
  const realTimeout = globalThis.setTimeout
  const realClear = globalThis.clearTimeout
  const timers: Array<{ fn: () => void; ms: number; cleared: boolean }> = []
  ;(globalThis as { setTimeout: unknown }).setTimeout = (fn: () => void, ms?: number) => {
    const timer = { fn, ms: ms ?? 0, cleared: false }
    timers.push(timer)
    return timer
  }
  ;(globalThis as { clearTimeout: unknown }).clearTimeout = (timer: { cleared: boolean }) => {
    if (timer) timer.cleared = true
  }
  try {
    const refreshOne = run(js, 'refreshOneAccount', { ref, t, accountStore, message })
    const acc = { steam_id: 'S2', persona_name: 'P2', friend_code: 'F2' }
    await refreshOne(acc)
    await refreshOne(acc)
    assert.deepEqual(calls, ['S2', 'S2'], '两次点击都应受理（第一拍已完成）')
    assert.ok(toasts.every((s) => s.startsWith('settings.toast.accountSyncQueued')), '成功提示应为「已触发同步」语义')
    assert.equal(timers.length, 2, '每次成功都应调度延迟重拉')
    assert.equal(timers[0]!.ms, 4000)
    assert.ok(timers[0]!.cleared, '第二次调度应顶替第一拍')
    timers[1]!.fn()
    await new Promise(setImmediate)
    assert.equal(loadCalls, 1, '延迟到点后重拉一次账号快照')

    /* 进行中挡并发：in-flight 时第二次点击不触发 */
    let pendingCalls = 0
    let resolvePending!: (v: unknown) => void
    const pendingStore = {
      refreshAccount() {
        pendingCalls++
        return new Promise((r) => { resolvePending = r })
      },
      load() { return Promise.resolve() },
    }
    const refreshPending = run(js, 'refreshOneAccount', {
      ref, t, accountStore: pendingStore,
      message: { success: () => {}, error: () => {} },
    })
    const inflight = refreshPending(acc)
    await refreshPending(acc)
    assert.equal(pendingCalls, 1, '进行中不得重复触发')
    resolvePending({ ok: true, steamId: 'S2', wallet: { ok: true, wallet: {} }, queued: [] })
    await inflight

    const failing = { refreshAccount: () => Promise.reject(new Error('HTTP 404')) }
    const errs: string[] = []
    const refreshFail = run(js, 'refreshOneAccount', {
      ref, t, accountStore: failing,
      message: { success: () => assert.fail('不应成功'), error: (s: string) => errs.push(s) },
    })
    await refreshFail(acc)
    assert.equal(errs.length, 1)
    assert.ok(errs[0]!.startsWith('settings.toast.accountSyncFailed'), '失败必须有错误提示')
  } finally {
    globalThis.setTimeout = realTimeout
    globalThis.clearTimeout = realClear
  }
})

/* ── account store：refreshAccount 走受理式端点（响应非整份快照，不 apply）── */

function accountStoreWith(api: Record<string, unknown>) {
  setActivePinia(createPinia())
  const source = readFileSync(new URL('../src/stores/account.ts', import.meta.url), 'utf8')
  const ast = ts.createSourceFile('account.ts', source, ts.ScriptTarget.Latest, true)
  const code = ast.statements.filter((s) => !ts.isImportDeclaration(s)).map((s) => s.getText(ast))
    .join('\n').replace(/^export /gm, '')
  const js = ts.transpileModule(code, { compilerOptions: { target: ts.ScriptTarget.Latest } }).outputText
  return run(js, 'useAccountStore()', {
    defineStore, ref, computed, accountApi: api, normalizeAvatarUrl: (v: string) => v,
  })
}

test('store.refreshAccount：调用受理式端点并原样返回结果（不整包 apply）', async () => {
  const seen: string[] = []
  const store = accountStoreWith({
    async status() { return statusOf('S1') },
    async refreshAccount(sid: string) {
      seen.push(sid)
      return { ok: true, steamId: sid, wallet: { ok: true, wallet: statusOf('S1').accounts[0]!.wallet }, queued: ['wishlist', 'family', 'bills'] }
    },
  })
  await store.load()
  const gameCountBefore = store.accounts[2]!.game_count
  const r = await store.refreshAccount('S3')
  assert.deepEqual(seen, ['S3'])
  assert.equal(r.ok, true)
  assert.equal(r.steamId, 'S3')
  assert.deepEqual(r.queued, ['wishlist', 'family', 'bills'])
  assert.equal(store.accounts[2]!.game_count, gameCountBefore, '受理式响应不得直接改写账号列表（等延迟重拉）')
})

test('store.setActive：切换后 is_active 随快照迁移，切换中并发被挡', async () => {
  let gate: Promise<ReturnType<typeof statusOf>> = Promise.resolve(statusOf('S1'))
  const store = accountStoreWith({
    async status() { return statusOf('S1') },
    async setActive(sid: string) { await gate; return statusOf(sid) },
  })
  await store.load()
  let resolveGate!: () => void
  gate = new Promise((r) => { resolveGate = r })
  const first = store.setActive('S2')
  await store.setActive('S3')
  assert.equal(store.active?.steam_id, 'S1', '切换中第二次调用应被 no-op')
  resolveGate()
  await first
  assert.equal(store.active?.steam_id, 'S2')
  assert.ok(store.accounts[1]!.is_active && !store.accounts[0]!.is_active)
})
