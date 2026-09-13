/* English 词典 · logs（与 zh-CN/logs.ts 同构，key 必须逐一对齐）。

   「已复制」按钮态复用 common.copied，此处不重复。 */

import type { MessageKey } from '../zh-CN'

const logs: Partial<Record<MessageKey, string>> = {
  /* SSE connection state */
  'logs.stream.connected': 'Live stream connected',
  'logs.stream.connecting': 'Connecting… (reconnects automatically)',

  /* Toolbar counters */
  'logs.count.lines': '{n} lines',
  'logs.count.errors': '{n} errors',
  'logs.count.warnings': '{n} warnings',

  /* Toolbar buttons */
  'logs.action.resume': 'Resume following',
  'logs.action.following': 'Following',
  'logs.action.copyAll': 'Copy all {n} lines',
  'logs.action.clear': 'Clear',

  /* Log box */
  'logs.empty': 'No log output yet — crawls, rate refreshes and the scheduler print here in real time',
  'logs.line.clickToCopy': 'Click to copy this line',

  /* Copy result toasts */
  'logs.copy.lineCopied': 'Line copied',
  'logs.copy.failed': 'Copy failed',
  'logs.copy.failedHint': 'Copy failed — select the text manually and press Ctrl+C',
}

export default logs
