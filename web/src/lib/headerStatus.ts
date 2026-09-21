/** 顶栏状态胶囊的产品级映射（纯展示层，不新增状态体系）。
 *
 *  输入是 crawlStatus store 已有的运行数据与「历史上是否收敛过」这一个信号，
 *  输出**一个**产品结论：正在更新 / 价格已更新 / 部分更新 / 等待更新。
 *  内部运行概念（job / 队列 / 速度 / worker / 代理 / 重试次数）不进主状态，
 *  也不进普通用户 tooltip——它们只在技术诊断入口里出现。
 *
 *  规则：
 *  - 正在跑 → running（有目标量时带 {done}/{total}）
 *  - 跑完且本轮有失败，或本轮**没跑完就停了** → partial
 *    （系统仍会自动重试，不表达成「更新失败」）
 *  - 跑完且无失败 → done
 *  - 没有活动：有历史 → idle（价格已更新）；从没跑过 → waiting（等待更新）
 */

export type PriceStatusKind = 'running' | 'done' | 'partial' | 'idle' | 'waiting'

export interface PriceStatusInput {
  running: boolean
  done: number
  fail: number
  total: number
  /** 最近一次任务的终态：done / failed / stopped（未知为 null） */
  lastStatus?: string | null
  /** 至少有过一次收敛的刷新周期，或本会话出现过活动：区分「已更新」与「还没更新过」 */
  hasHistory: boolean
}

export interface PriceStatus {
  kind: PriceStatusKind
  /** 已处理款数（本轮进度口径，与后端 stats 的 done 同源） */
  done: number
  /** 本轮目标款数；未知为 0 */
  total: number
  /** 本轮没拿到、稍后会自动重试的款数 */
  retry: number
}

function count(value: number): number {
  return Number.isFinite(value) && value > 0 ? Math.floor(value) : 0
}

export function priceStatusOf(input: PriceStatusInput): PriceStatus {
  const done = count(input.done)
  const total = count(input.total)
  const fail = count(input.fail)
  const unfinished = input.lastStatus === 'stopped' || input.lastStatus === 'failed'

  let kind: PriceStatusKind
  if (input.running) {
    kind = 'running'
  } else if (total > 0) {
    // 本轮有目标量：有失败或没跑完 → 部分更新；否则算完成
    kind = fail > 0 || unfinished ? 'partial' : 'done'
  } else {
    // 本轮没有目标量（还没跑过，或本轮没产生进度事件）
    kind = input.hasHistory ? 'idle' : 'waiting'
  }
  return { kind, done, total, retry: fail }
}