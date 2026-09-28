/** 价格更新状态的产品级映射（纯展示层，不新增状态体系）。
 *
 *  输入是「最近一轮刷新的终态」与「是否存在写入过价格的轮次」两个信号，
 *  输出**一个**产品结论：正在更新 / 价格已更新 / 更新未完成 / 等待更新。
 *
 *  结论以**刷新轮次**（price_cycles）为准：一轮的终态由该轮全部任务的结果
 *  决定，不是最后一个任务的结果——末位任务跑成不代表这轮收敛。store 的实时
 *  计数只在拿不到轮次时兜底（轮次建行与请求之间有空档）。
 *
 *  计数口径约束：store 的 ok / fail / total 统计的是**更新任务批次**
 *  （1 批 = 1 地区 × ≤400 款），不是游戏数——本模块只消费成败结论，
 *  不对外产出任何数量；批次数不得以「款 / 游戏」为单位进入用户面。
 *  内部运行概念（job / 队列 / 速度 / worker / 代理 / 重试次数）不进主状态，
 *  也不进普通用户 tooltip——它们只在技术诊断入口里出现。
 *
 *  规则：
 *  - 正在跑（实时计数或轮次未收束）→ running
 *  - 最近一轮是未收敛终态（失败 / 部分 / 取消）→ partial
 *  - 本轮有产出（有成功 / 有目标量 / 有失败）：
 *    有失败 → partial；否则 → done
 *  - 没有活动：有历史 → idle（价格已更新）；从未写入过 → waiting（等待更新）
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
  /** 最近一轮刷新周期的终态（completed / failed / partial / cancelled / planning…）；未知为 null */
  lastStatus?: string | null
  /** 存在写入过价格的轮次，或本会话出现过活动：区分「已更新」与「还没更新过」 */
  hasHistory: boolean
}

/** 轮次已建行但尚未收束：此间实时计数还没跟上，结论按「正在更新」给 */
export const CYCLE_ACTIVE = new Set(['planning', 'running', 'repairing', 'finalizing'])

/** 未收敛的轮次终态：跑完了但没拿满，或中途失败 / 取消——都不得声称「价格已更新」 */
const CYCLE_UNCONVERGED = new Set(['failed', 'partial', 'cancelled', 'stopped'])

function count(value: number): number {
  return Number.isFinite(value) && value > 0 ? Math.floor(value) : 0
}

export function priceStatusOf(input: PriceStatusInput): PriceStatusKind {
  const ok = count(input.ok)
  const fail = count(input.fail)
  const total = count(input.total)
  const last = input.lastStatus ?? null

  if (input.running) return 'running'
  if (last !== null && CYCLE_ACTIVE.has(last)) return 'running'
  if (last !== null && CYCLE_UNCONVERGED.has(last)) return 'partial'

  // 拿不到轮次时的兜底：本轮是否已有产出，决定「跑过」还是「没动过」
  if (ok > 0 || total > 0 || fail > 0) {
    return fail > 0 ? 'partial' : 'done'
  }
  return input.hasHistory ? 'idle' : 'waiting'
}
