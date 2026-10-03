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

export type FreshnessBucket = 'fresh' | 'lagging' | 'stale'

/** 新鲜度色档（对象级）。阈值与后端 freshness.py 同一契约：
    fresh <6h / lagging <12h / stale ≥12h，两边必须同步改。
    独立于 agePart 的展示分档——这是后端同名的档位口径，供色档使用。 */
export function freshnessBucket(ageHours: number): FreshnessBucket {
  if (ageHours < 6) return 'fresh'
  if (ageHours < 12) return 'lagging'
  return 'stale'
}


export interface CoverageView extends TextPart {
  /** 未刷满：文案已点明「未完整」，样式据此上色 */
  partial: boolean
}

/** 覆盖率 → 文案；无覆盖数据或**已刷满**都返回 null——
 * 成功观察数达到期望区数时不出覆盖标签，价格数据行只剩观察时间。
 * 未刷满才显示「覆盖 x/y，未完整」，悬停点名问题地区（tips）。
 *
 * 覆盖口径（活表尝试状态）：成功观察 = 拿到 Steam 明确答复（含 locked /
 * 无购买选项）；failed = 传输类失败；notAttempted = 尚无尝试记录。 */
export function coverageView(cov: PriceCoverage | null | undefined): CoverageView | null {
  if (!cov) return null
  if (cov.success >= cov.expectedUnits) return null
  return {
    key: 'gameCard.priceData.coveragePartial',
    params: { ok: cov.success, expected: cov.expectedUnits },
    partial: true,
  }
}

/** ISO 时刻 → 短时间文案（月-日 时:分）。纯展示分档用，不做时区语义。 */
function shortTime(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** 未覆盖部分 → 悬停提示。区级明细逐区点名：
 * 本次失败（带上次成功时刻）→ 「本次抓取失败，展示 X 时刻的数据」；
 * 未尝试 → 「还没抓过」。成功区（含锁区）不点名。 */
export function coverageTipParts(
  cov: PriceCoverage | null | undefined,
  regionName?: (code: string) => string,
): TextPart[] {
  if (!cov) return []
  const parts: TextPart[] = []
  for (const [code, info] of Object.entries(cov.regions ?? {})) {
    const name = regionName?.(code) ?? code
    if (info.outcome === 'failed') {
      if (info.lastSuccessAt) {
        parts.push({
          key: 'gameCard.priceData.tipRegion.failedStale',
          params: { region: name, time: shortTime(info.lastSuccessAt) },
        })
      } else {
        parts.push({ key: 'gameCard.priceData.tipRegion.failed', params: { region: name } })
      }
    } else {
      parts.push({ key: 'gameCard.priceData.tipRegion.notAttempted', params: { region: name } })
    }
  }
  return parts
}

/** 价格数据状态 → 卡片要渲染的全部内容。
 * regionName：区码 → 展示名（调用方给，本模块不依赖 store / i18n）。 */
export function priceDataView(
  data: PriceData | null | undefined,
  regionName?: (code: string) => string,
): {
  age: TextPart
  coverage: CoverageView | null
  tips: TextPart[]
  freshness: PriceData['freshness']
} {
  return {
    age: agePart(data?.ageHours),
    coverage: coverageView(data?.coverage),
    tips: coverageTipParts(data?.coverage, regionName),
    freshness: data?.freshness ?? null,
  }
}
