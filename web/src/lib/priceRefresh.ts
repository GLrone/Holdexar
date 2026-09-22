/**
 * 价格周期收敛后的原地刷新规则（纯函数，不依赖 Vue）。
 *
 * 触发单位是 Price Refresh Cycle，不是 crawl_job：一轮里有多个 job，按 job
 * 触发会把同一次刷新放大成多次请求。
 */

export interface PriceCycleMark {
  cycleId: number
  status: string
}

/**
 * SSE `price_cycle.completed` 载荷 → 新的周期标记；null = 不推进。
 *
 * 同一轮重复到达（EventSource 重连/重放）返回 null，调用方据此跳过刷新——
 * 重复事件不会变成重复请求，也不会无限刷新。
 */
export function nextPriceCycle(current: PriceCycleMark | null, data: unknown): PriceCycleMark | null {
  if (typeof data !== 'object' || data === null) return null
  const payload = data as { cycleId?: unknown; status?: unknown }
  const cycleId = Number(payload.cycleId)
  if (!Number.isFinite(cycleId) || cycleId === current?.cycleId) return null
  return { cycleId, status: String(payload.status ?? '') }
}

/**
 * 按 appid 就地替换条目数据：顺序与数量都不变，只换命中的条目。
 *
 * 不重排、不增删，否则卡片会被卸载重挂——用户正在打开的价格走势抽屉、
 * 弹层、以及滚动位置都会跟着丢。刷新拿不到的对象保留原数据（下一次自然
 * 翻页/换参时更新）。
 */
export function mergeItemsByAppid<T extends { appid: number }>(
  current: readonly T[],
  fresh: ReadonlyMap<number, T>,
): T[] {
  return current.map((item) => fresh.get(item.appid) ?? item)
}
