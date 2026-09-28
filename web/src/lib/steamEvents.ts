import type { SteamEventItem } from '@/api/client'

/**
 * Steam 活动时刻口径。
 *
 * 官方文档的活动日期是 PT 时区的活动日，开始时刻按 Valve 惯例为当天
 * 10:00 PT。PT 在夏令时（PDT，UTC-7，3 月第二个周日 → 11 月第一个周日）
 * 与冬令时（PST，UTC-8）间切换，对北京时间即活动开始于当天凌晨 1 点
 * （夏令时）/ 2 点（冬令时）——与调度器价格刷新锚（北京 01:00[夏令时]/
 * 02:00[冬令时]）同一语义。商店层回填精确时间戳（preciseStartTs）后
 * 直接使用精确值，此处换算只在缺精确值时生效。
 */

/** 日期（YYYY-MM-DD）当天 10:00 PT 是否处于夏令时（PDT）。
 *  边界日按「10:00 已在切换后」处理：3 月第二个周日起算 PDT，
 *  11 月第一个周日起算 PST。 */
function isPacificDst(dateIso: string): boolean {
  const [y, m, d] = dateIso.split('-').map(Number)
  if (!y || !m || !d) return false
  if (m < 3 || m > 11) return false
  if (m > 3 && m < 11) return true
  const firstDow = new Date(Date.UTC(y, m - 1, 1)).getUTCDay()
  if (m === 3) {
    const secondSunday = 8 + ((7 - firstDow) % 7)
    return d >= secondSunday
  }
  const firstSunday = 1 + ((7 - firstDow) % 7)
  return d < firstSunday
}

/** 活动开始时刻（毫秒）：精确时间戳优先，否则按 10:00 PT 换算。 */
export function eventStartTs(e: SteamEventItem): number {
  if (e.preciseStartTs) return e.preciseStartTs * 1000
  const offset = isPacificDst(e.start) ? 7 : 8
  const [y, m, d] = e.start.split('-').map(Number)
  return Date.UTC(y, (m || 1) - 1, d || 1, 10 + offset, 0, 0)
}

/** 活动结束时刻（毫秒）：精确时间戳优先，否则按结束日当天 10:00 PT 换算
 *  （官方窗口为「起始日 10:00 → 结束日 10:00」）。 */
export function eventEndTs(e: SteamEventItem): number {
  if (e.preciseEndTs) return e.preciseEndTs * 1000
  const offset = isPacificDst(e.end) ? 7 : 8
  const [y, m, d] = e.end.split('-').map(Number)
  return Date.UTC(y, (m || 1) - 1, d || 1, 10 + offset, 0, 0)
}

function shiftIso(iso: string, days: number): string {
  const [y, m, d] = iso.split('-').map(Number)
  return new Date(Date.UTC(y, (m || 1) - 1, (d || 1) + days)).toISOString().slice(0, 10)
}

/** 界面展示日期：中文语境显示北京时间窗口——活动窗口为 PT 日 10:00 起
 *  止，对北京时间恒为次日凌晨 1/2 点，起止整体 +1 天；英文语境保持
 *  PT 活动日。倒计时与状态短语都应对齐这里展示的日期。 */
export function displayRangeIso(
  e: SteamEventItem,
  locale: string,
): { start: string; end: string } {
  if (locale !== 'zh-CN') return { start: e.start, end: e.end }
  return { start: shiftIso(e.start, 1), end: shiftIso(e.end, 1) }
}
