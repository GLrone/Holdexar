import assert from 'node:assert/strict'
import { test } from 'node:test'
import { amountToMinor, minorToInput } from '../src/lib/notificationForms.ts'

test('notification amounts use exact stored cents for every currency', () => {
  for (const [input, expected] of [['12.34', 1234], ['0.29', 29], ['100', 10000], ['0', 0], [' 1.2 ', 120]] as const) {
    assert.equal(amountToMinor(input), expected)
    assert.equal(amountToMinor(minorToInput(expected)), expected)
  }
})

test('invalid, excessive precision and unsafe notification amounts are rejected', () => {
  for (const input of ['', '-1', '1e3', '1.005', 'NaN', 'Infinity', '1,000', '9007199254740992']) {
    assert.equal(amountToMinor(input), null)
  }
  assert.equal(minorToInput(null), '')
})
