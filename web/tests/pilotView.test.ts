import assert from 'node:assert/strict'
import { test } from 'node:test'

import { canRateTurn, fmtTokens, isEmptyLlmFailure, isErrorReason, isLongAnswerText, positivePct, ratingClass } from '../src/lib/pilotView.ts'

test('有可见内容且有账本 id 的回答允许评分（含只有步骤或卡片的回答）', () => {
  assert.equal(canRateTurn({ tid: 'turn-1', text: '回答' }), true)
  assert.equal(canRateTurn({ tid: 'turn-2', steps: [{}] }), true)
  assert.equal(canRateTurn({ tid: 'turn-3', cards: [{}] }), true)
  assert.equal(canRateTurn({ tid: 'turn-4', text: '' }), false)
  assert.equal(canRateTurn({ text: '没有可保存的账本轮次' }), false)
})

test('positivePct：0–1 小数按百分整数渲染', () => {
  assert.equal(positivePct(0.872), 87)
  assert.equal(positivePct(0.92), 92)
  assert.equal(positivePct(0.55), 55)
})

test('positivePct：边界与空值', () => {
  assert.equal(positivePct(1), 100)
  assert.equal(positivePct(null), null)
  assert.equal(positivePct(0), null)
  assert.equal(positivePct(undefined), null)
})

test('ratingClass：分级阈值与游戏卡一致（≥80 / ≥60 / 其余）', () => {
  assert.equal(ratingClass(0.872), '')
  assert.equal(ratingClass(0.92), '')
  assert.equal(ratingClass(0.55), 'low')
  assert.equal(ratingClass(0.7), 'medium')
  assert.equal(ratingClass(null), 'low')
})

test('fmtTokens：千位缩写 K 一位小数（上下文占用与记忆条目共用）', () => {
  assert.equal(fmtTokens(999), '999')
  assert.equal(fmtTokens(1000), '1.0K')
  assert.equal(fmtTokens(211), '211')
  assert.equal(fmtTokens(6000), '6.0K')
})

test('isLongAnswerText：700 字符阈值边界与空值', () => {
  assert.equal(isLongAnswerText(null), false)
  assert.equal(isLongAnswerText(undefined), false)
  assert.equal(isLongAnswerText(''), false)
  assert.equal(isLongAnswerText('a'.repeat(700)), false)
  assert.equal(isLongAnswerText('a'.repeat(701)), true)
  assert.equal(isLongAnswerText('a'.repeat(2000)), true)
})

test('长回答展开与收敛策略：最新轮次默认展开，发新对话收敛历史轮次', () => {
  interface MockTurn {
    text: string
    kind: string
    expanded?: boolean
  }

  const turns: MockTurn[] = []

  // 1. 用户发第 1 轮
  for (const t of turns) t.expanded = false
  turns.push({ text: '', kind: 'pending' })

  // 第 1 轮完成：长回答默认展开
  const turn1: MockTurn = { text: 'a'.repeat(1200), kind: 'answer', expanded: true }
  turns.splice(turns.length - 1, 1, turn1)
  assert.equal(turns.length, 1)
  assert.equal(turns[0].expanded, true)
  assert.equal(isLongAnswerText(turns[0].text), true)

  // 2. 用户发第 2 轮（发新对话）：收敛上一次输出结果
  for (const t of turns) t.expanded = false
  turns.push({ text: '', kind: 'pending' })
  assert.equal(turns[0].expanded, false) // 上一次输出结果已收敛

  // 第 2 轮完成：最新对话结果默认展开
  const turn2: MockTurn = { text: 'b'.repeat(900), kind: 'answer', expanded: true }
  turns.splice(turns.length - 1, 1, turn2)
  assert.equal(turns.length, 2)
  assert.equal(turns[0].expanded, false) // 历史保持收敛
  assert.equal(turns[1].expanded, true)  // 最新结果默认展开

  // 3. 用户发第 3 轮：本次和上一次输出结果全部收敛
  for (const t of turns) t.expanded = false
  turns.push({ text: '', kind: 'pending' })
  assert.equal(turns[0].expanded, false)
  assert.equal(turns[1].expanded, false)

  // 第 3 轮完成：最新结果默认展开
  const turn3: MockTurn = { text: 'c'.repeat(800), kind: 'answer', expanded: true }
  turns.splice(turns.length - 1, 1, turn3)
  assert.equal(turns[0].expanded, false)
  assert.equal(turns[1].expanded, false)
  assert.equal(turns[2].expanded, true)
})

test('会话恢复策略：历史轮次收敛，最新一轮对话结果默认展开', () => {
  const loaded = [
    { text: 'q1 answer '.repeat(100), kind: 'answer', expanded: false },
    { text: 'q2 answer '.repeat(100), kind: 'answer', expanded: false },
  ]
  for (let i = loaded.length - 1; i >= 0; i--) {
    if (loaded[i].kind === 'answer' || loaded[i].text) {
      loaded[i].expanded = true
      break
    }
  }
  assert.equal(loaded[0].expanded, false)
  assert.equal(loaded[1].expanded, true)
})

test('isErrorReason：失败异常原因判定', () => {
  assert.equal(isErrorReason('pilot.error'), true)
  assert.equal(isErrorReason('pilot.reason.llm_failed'), true)
  assert.equal(isErrorReason('pilot.reason.llm_failed_empty'), true)
  assert.equal(isErrorReason('pilot.reason.busy'), true)
  assert.equal(isErrorReason('pilot.reason.too_long'), true)
  assert.equal(isErrorReason('pilot.reason.cap_reached'), true)
  assert.equal(isErrorReason('pilot.reason.max_steps'), true)
  // 非异常原因
  assert.equal(isErrorReason('pilot.reason.need_target'), false)
  assert.equal(isErrorReason('pilot.reason.llm_off'), false)
  assert.equal(isErrorReason(null), false)
  assert.equal(isErrorReason(undefined), false)
  assert.equal(isErrorReason(''), false)
})

test('isEmptyLlmFailure：无事实兜底时使用无回答文案', () => {
  assert.equal(isEmptyLlmFailure('llm_failed', 'none'), true)
  assert.equal(isEmptyLlmFailure('llm_failed', 'facts'), false)
  assert.equal(isEmptyLlmFailure('no_data', 'none'), false)
})
