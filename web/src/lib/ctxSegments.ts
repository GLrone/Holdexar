/**
 * 领航台上下文分段条的数据层：后端 done.ctx_breakdown（五来源字符数）→
 * 占比段排序 + 缓存命中率展示阈值。纯函数，供组件与单测共用。
 */

export interface CtxSourceRow {
  source: string
  chars: number
}

export interface CtxSegment {
  source: string
  /** 占全部字符的份额（0-1），降序；同份额按固定来源序稳定排列 */
  share: number
}

/** 来源固定序（份额相同时的稳定排列依据）；未知来源视为排末尾 */
export const CTX_SOURCE_ORDER = ['system', 'tools', 'summary', 'history', 'current'] as const

function orderOf(source: string): number {
  const idx = CTX_SOURCE_ORDER.indexOf(source as (typeof CTX_SOURCE_ORDER)[number])
  return idx === -1 ? CTX_SOURCE_ORDER.length : idx
}

/** 缓存命中率展示阈值：低命中不展示，避免分散对上下文容量的注意力 */
export const CACHE_HIT_RATE_DISPLAY_THRESHOLD = 0.78

export function buildCtxSegments(breakdown: CtxSourceRow[] | null | undefined): CtxSegment[] {
  if (!breakdown?.length) return []
  const rows = breakdown.filter((r) => Number.isFinite(r.chars) && r.chars > 0)
  const total = rows.reduce((sum, r) => sum + r.chars, 0)
  if (total <= 0) return []
  return rows
    .map((r) => ({ source: r.source, share: r.chars / total }))
    .sort((a, b) => b.share - a.share || orderOf(a.source) - orderOf(b.source))
}

export function shouldShowCacheHitRate(rate: number | null | undefined, dev = false): boolean {
  if (typeof rate !== 'number' || !Number.isFinite(rate)) return false
  return dev || rate >= CACHE_HIT_RATE_DISPLAY_THRESHOLD
}
