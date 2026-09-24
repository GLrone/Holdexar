/** 顶栏状态胶囊的产品级映射（纯展示层，不新增状态体系）。
 *
 *  输入是 crawlStatus store 已有的运行数据与「历史上是否收敛过」这一个信号，
 *  输出**一个**产品结论：正在更新 / 价格已更新 / 部分更新 / 等待更新。
 *
 *  计数口径约束：store 的 ok / fail / total 统计的是**更新任务批次**
 *  （1 批 = 1 地区 × ≤400 款），不是游戏数——本模块只消费成败结论，
 *  不对外产出任何数量；批次数不得以「款 / 游戏」为单位进入用户面。
 *  内部运行概念（job / 队列 / 速度 / worker / 代理 / 重试次数）不进主状态，
 *  也不进普通用户 tooltip——它们只在技术诊断入口里出现。
 *
 *  规则：
 *  - 正在跑 → running
 *  - 本轮有产出（有目标量 / 有失败 / 没跑完就停）：
 *    有失败或没跑完 → partial（系统仍会自动重试，不表达成「更新失败」）；
 *    否则 → done
 *  - 没有活动：有历史 → idle（价格已更新）；从没跑过 → waiting（等待更新）
 */

export type PriceStatusKind = 'running' | 'done' | 'partial' | 'idle' | 'waiting'

export interface PriceStatusInput {
  running: boolean
  /** 成功批次（SSE ok）；批次口径，不是成功游戏数 */
  ok: number
  /** 失败批次（含失败账本） */
  fail: number
  /** 本轮目标批次；未知为 0 */
  total: number
  /** 最近一次任务的终态：done / failed / stopped（未知为 null） */
  lastStatus?: string | null
  /** 至少有过一次收敛的刷新周期，或本会话出现过活动：区分「已更新」与「还没更新过」 */
  hasHistory: boolean
}

function count(value: number): number {
  return Number.isFinite(value) && value > 0 ? Math.floor(value) : 0
}

export function priceStatusOf(input: PriceStatusInput): PriceStatusKind {
  const fail = count(input.fail)
  const total = count(input.total)
  const unfinished = input.lastStatus === 'stopped' || input.lastStatus === 'failed'

  if (input.running) return 'running'
  if (total > 0 || fail > 0 || unfinished) {
    // 本轮有产出：有失败或没跑完 → 部分更新；否则算完成
    return fail > 0 || unfinished ? 'partial' : 'done'
  }
  return input.hasHistory ? 'idle' : 'waiting'
}
