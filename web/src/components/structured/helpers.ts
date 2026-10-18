/**
 * Holdexar Structured UI System — 展示数据计算与格式化共享工具。
 */
import { formatCnyFen } from '@/api/regions'
import { positivePct, ratingClass } from '@/lib/pilotView'
import type { PilotFacts, PilotPriceFacts } from '@/api/client'
import type { MessageKey } from '@/locales'

export function fen(v: number | null | undefined): string {
  return typeof v === 'number' && v > 0 ? formatCnyFen(v) : '—'
}

export function ratingText(rate: number | null | undefined): string | null {
  const pct = positivePct(rate)
  return pct === null ? null : `${pct}%`
}

const countFmt = new Map<string, Intl.NumberFormat>()
export function fmtCount(n: number | null | undefined, locale = 'zh-CN'): string {
  if (!n) return ''
  let f = countFmt.get(locale)
  if (!f) {
    f = new Intl.NumberFormat(locale, { notation: 'compact' })
    countFmt.set(locale, f)
  }
  return f.format(n)
}

export function dateOf(iso: string | null | undefined): string {
  return (iso || '').slice(0, 10)
}

export function rangePos(f: PilotPriceFacts): number | null {
  const cur = f.cn?.cnyFen
  const y = f.year
  if (!y || y.maxFen <= y.minFen || typeof cur !== 'number' || cur <= 0) return null
  return Math.max(0, Math.min(100, Math.round(((cur - y.minFen) / (y.maxFen - y.minFen)) * 100)))
}

export function medianPos(f: PilotPriceFacts): number | null {
  const y = f.year
  if (!y || y.maxFen <= y.minFen) return null
  return Math.max(0, Math.min(100, Math.round(((y.medianFen - y.minFen) / (y.maxFen - y.minFen)) * 100)))
}

export interface DealAdvice {
  tone: 'good' | 'warn' | 'accent' | 'neutral'
  labelKey: MessageKey
}

export function getDealAdvice(f: PilotPriceFacts): DealAdvice | null {
  const cur = f.cn?.cnyFen
  const lowest = f.lowest?.cnyFen
  const y = f.year
  if (typeof cur !== 'number' || cur <= 0) return null

  if (typeof lowest === 'number' && lowest > 0 && cur <= lowest) {
    return { tone: 'good', labelKey: 'pilot.card.advice.atl' }
  }
  if (y && typeof y.medianFen === 'number' && y.medianFen > 0 && cur < y.medianFen) {
    return { tone: 'good', labelKey: 'pilot.card.advice.belowMedian' }
  }
  if (f.cn?.discount && f.cn.discount >= 50) {
    return { tone: 'accent', labelKey: 'pilot.card.advice.deepDiscount' }
  }
  if (y && typeof y.maxFen === 'number' && cur >= y.maxFen) {
    return { tone: 'warn', labelKey: 'pilot.card.advice.nearHigh' }
  }
  return null
}
