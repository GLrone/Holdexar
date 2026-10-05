import assert from 'node:assert/strict'
import { test } from 'node:test'

import { fmtTokens, positivePct, ratingClass } from '../src/lib/pilotView.ts'

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
