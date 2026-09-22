import type { MessageKey } from '@/locales'
import type { PriceCoverage, PriceData } from '@/api/client'

/**
 * 价格数据状态的展示规则（纯函数，不依赖 Vue / i18n）。
 *
 * 三条措辞红线集中在这里，卡片与测试共用同一份判定：
 * - 锁区不是抓取失败；「本轮没结果」也不等于「价格不可用」——三者分开说。
 * - 未刷满不写成已完成（数字照给，另标 partial）。
 * - 没有 Cycle 归属（不属本轮期望集）就不给覆盖率，不冒充 100%。
 */

export interface TextPart {
  key: MessageKey
  params?: Record<string, number>
}

/** 相对时长的词条组：分档逻辑只有一份，措辞由调用场景给 */
export interface AgeKeys {
  none: MessageKey
  justNow: MessageKey
  minutes: MessageKey
  hours: MessageKey
  days: MessageKey
}

const CARD_AGE_KEYS: AgeKeys = {
  none: 'gameCard.priceData.none',
  justNow: 'gameCard.priceData.justNow',
  minutes: 'gameCard.priceData.minutesAgo',
  hours: 'gameCard.priceData.hoursAgo',
  days: 'gameCard.priceData.daysAgo',
}

/** 观察时间 → 相对时长词条。纯展示分档，与后端 freshness 阈值（6h/12h）无关 */
export function agePart(
  ageHours: number | null | undefined,
  keys: AgeKeys = CARD_AGE_KEYS,
): TextPart {
  if (ageHours === null || ageHours === undefined) {
    return { key: keys.none }
  }
  const minutes = Math.floor(ageHours * 60)
  if (minutes < 1) return { key: keys.justNow }
  if (minutes < 60) return { key: keys.minutes, params: { n: minutes } }
  if (ageHours < 48) return { key: keys.hours, params: { n: Math.floor(ageHours) } }
  return { key: keys.days, params: { n: Math.floor(ageHours / 24) } }
}


export interface CoverageView extends TextPart {
  /** 未刷满：文案已点明「未完整」，样式据此上色 */
  partial: boolean
}

/** 覆盖率 → 文案；无 Cycle 归属返回 null（不展示覆盖） */
export function coverageView(cov: PriceCoverage | null | undefined): CoverageView | null {
  if (!cov) return null
  const params = { ok: cov.ok, expected: cov.expectedUnits }
  return cov.ok >= cov.expectedUnits
    ? { key: 'gameCard.priceData.coverageFull', params, partial: false }
    : { key: 'gameCard.priceData.coveragePartial', params, partial: true }
}

/** 未覆盖部分的构成：锁区 / 抓取失败 / 本轮没结果各成一条，不合并成「失败」 */
export function coverageTipParts(cov: PriceCoverage | null | undefined): TextPart[] {
  if (!cov) return []
  const parts: TextPart[] = []
  if (cov.locked > 0) parts.push({ key: 'gameCard.priceData.tip.locked', params: { n: cov.locked } })
  const failed = cov.missing + cov.blocked
  if (failed > 0) parts.push({ key: 'gameCard.priceData.tip.failed', params: { n: failed } })
  if (cov.unobserved > 0) {
    parts.push({ key: 'gameCard.priceData.tip.unobserved', params: { n: cov.unobserved } })
  }
  return parts
}

/** 价格数据状态 → 卡片要渲染的全部内容 */
export function priceDataView(data: PriceData | null | undefined): {
  age: TextPart
  coverage: CoverageView | null
  tips: TextPart[]
  freshness: PriceData['freshness']
} {
  return {
    age: agePart(data?.ageHours),
    coverage: coverageView(data?.coverage),
    tips: coverageTipParts(data?.coverage),
    freshness: data?.freshness ?? null,
  }
}
