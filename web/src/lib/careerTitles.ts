/** 游戏生涯 · 称号名片与生涯评分（五级阶梯制）。
 *
 *  设计约束：本文件只放**度量口径与阈值**，不放任何译文——`nameKey` /
 *  `scopeKey` 存词典 key，渲染期由组件 `t()` 求值（模块级 const 只求值一次，
 *  会把语言冻在模块加载那一刻）。`*Key` 属性是 dotted 值，会被
 *  `scripts/check-i18n.mjs` 逐条校验存在性，漏 key 直接构建失败。
 *
 *  称号名片：**一系度量一张名片**，每张名片五级阶梯，升满五阶即
 *  「鎏金」档——鎏金是唯一配流动金边 + 扫光的档位，紫色（史诗）无扫光。
 */

import type { CareerPayload } from '@/api/client'
import type { IconName } from '@/components/ui/icons'
import type { MessageKey } from '@/locales'

export type CareerTitleCategory =
  | 'playtime'
  | 'library'
  | 'trophy'
  | 'platinum'
  | 'rarity'
  | 'activity'
  | 'taste'

/** 名片档位：随阶位升级；鎏金（五阶满）是唯一「珍贵动效」档 */
export type CareerCardTier = 'common' | 'rare' | 'epic' | 'legendary' | 'gilded'

/** 度量单位：决定卡片上数值的格式化方式 */
export type CareerMetricUnit =
  | 'count'
  | 'hour'
  | 'min'
  | 'percent'
  | 'fen'
  | 'fenPerHour'
  | 'perHour'
  | 'day'

export interface CareerCardSpec {
  id: string
  category: CareerTitleCategory
  icon: IconName
  nameKey: MessageKey
  /** 度量的对象（口径短句，多张名片共用同一批 scope 词条） */
  scopeKey: MessageKey
  unit: CareerMetricUnit
  metric: (c: CareerPayload) => number
  /** 五级阶梯，严格递增；五级为满（鎏金） */
  thresholds: readonly [number, number, number, number, number]
  /** 越小越强（用时 / 成本 / 平均稀有度）：进度按 target / value 计 */
  inverse?: boolean
}

export interface CareerTitleCard extends CareerCardSpec {
  value: number
  /** 0~5；0 = 连一阶都未达成 */
  level: number
  /** 级内进度 0~1；满阶恒 1 */
  progress: number
  /** 当前级目标（= 下一阶阈值）；满阶为 null */
  target: number | null
  maxed: boolean
  tier: CareerCardTier | null
}

export const CATEGORY_ORDER: CareerTitleCategory[] = [
  'playtime',
  'trophy',
  'platinum',
  'rarity',
  'activity',
  'taste',
  'library',
]

export const CARD_TIER_ORDER: CareerCardTier[] = [
  'gilded',
  'legendary',
  'epic',
  'rare',
  'common',
]

const KEY = 'achievements.career.title'

/** 阶位 → 档位；未入阶返回 null（不挂任何档色） */
export function cardTierOf(level: number): CareerCardTier | null {
  if (level >= 5) return 'gilded'
  if (level >= 4) return 'legendary'
  if (level >= 3) return 'epic'
  if (level >= 2) return 'rare'
  if (level >= 1) return 'common'
  return null
}

/** 便捷取数：全部来自后端 career 载荷，缺字段一律退化为 0 而不是抛错 */
const totalHours = (c: CareerPayload) => c.playtime.totalMin / 60
const singleHours = (c: CareerPayload) => c.playtime.maxMin / 60

/** 称号名片：一系度量一张卡，五级阶梯逐级升级。
 *  阈值方向：大部分游戏都有成就、白金并非稀缺物，上限整体抬高——
 *  一阶是「入门认证」，鎏金五阶只留给殿堂级的账号。 */
export const TITLE_CARDS: CareerCardSpec[] = [
  // ── 时长 ───────────────────────────────────────────────
  { id: 'hours', category: 'playtime', icon: 'play', unit: 'hour', thresholds: [10, 300, 1500, 3500, 7000], metric: totalHours, nameKey: `${KEY}.card.hours.name`, scopeKey: `${KEY}.scope.totalPlaytime` },
  { id: 'single', category: 'playtime', icon: 'gamepad', unit: 'hour', thresholds: [10, 60, 150, 300, 600], metric: singleHours, nameKey: `${KEY}.card.single.name`, scopeKey: `${KEY}.scope.singleGame` },
  { id: 'deep', category: 'playtime', icon: 'layers', unit: 'count', thresholds: [1, 5, 12, 25, 40], metric: (c) => c.playtime.over100h, nameKey: `${KEY}.card.deep.name`, scopeKey: `${KEY}.scope.gamesOver100h` },

  // ── 奖杯 ───────────────────────────────────────────────
  { id: 'collector', category: 'trophy', icon: 'trophy', unit: 'count', thresholds: [100, 1000, 5000, 15000, 30000], metric: (c) => c.trophy.unlocked, nameKey: `${KEY}.card.collector.name`, scopeKey: `${KEY}.scope.totalTrophy` },
  { id: 'perfectionist', category: 'trophy', icon: 'target', unit: 'percent', thresholds: [5, 15, 30, 50, 70], metric: (c) => c.trophy.rate, nameKey: `${KEY}.card.perfectionist.name`, scopeKey: `${KEY}.scope.completionRate` },
  { id: 'efficient', category: 'trophy', icon: 'zap', unit: 'perHour', thresholds: [0.5, 1.5, 3, 5, 8], metric: (c) => c.trophy.perHour, nameKey: `${KEY}.card.efficient.name`, scopeKey: `${KEY}.scope.trophyPerHour` },
  { id: 'closer', category: 'trophy', icon: 'check-circle', unit: 'count', thresholds: [1, 3, 8, 15, 25], metric: (c) => c.unfinished.filter((g) => g.remaining <= 5).length, nameKey: `${KEY}.card.closer.name`, scopeKey: `${KEY}.scope.nearPerfect` },

  // ── 白金 ───────────────────────────────────────────────
  { id: 'plathunter', category: 'platinum', icon: 'trophy', unit: 'count', thresholds: [20, 50, 100, 180, 300], metric: (c) => c.platinum.count, nameKey: `${KEY}.card.plathunter.name`, scopeKey: `${KEY}.scope.platinumCount` },
  { id: 'voyager', category: 'platinum', icon: 'calendar', unit: 'day', thresholds: [30, 180, 365, 730, 1460], metric: (c) => c.platinum.spanDays, nameKey: `${KEY}.card.voyager.name`, scopeKey: `${KEY}.scope.platinumSpan` },

  // ── 稀有 ───────────────────────────────────────────────
  { id: 'rarehunter', category: 'rarity', icon: 'star', unit: 'count', thresholds: [10, 50, 150, 350, 600], metric: (c) => c.trophy.rareCount, nameKey: `${KEY}.card.rarehunter.name`, scopeKey: `${KEY}.scope.veryRareCount` },
  { id: 'ultrahunter', category: 'rarity', icon: 'zap', unit: 'count', thresholds: [1, 5, 15, 40, 80], metric: (c) => c.trophy.rarity.ultra, nameKey: `${KEY}.card.ultrahunter.name`, scopeKey: `${KEY}.scope.ultraRareCount` },
  { id: 'hardened', category: 'rarity', icon: 'target', unit: 'percent', thresholds: [45, 30, 20, 12, 6], inverse: true, metric: (c) => (c.trophy.unlocked >= 100 ? c.trophy.avgRarity : 100), nameKey: `${KEY}.card.hardened.name`, scopeKey: `${KEY}.scope.avgRarity` },

  // ── 活跃 ───────────────────────────────────────────────
  { id: 'streaker', category: 'activity', icon: 'zap', unit: 'day', thresholds: [7, 30, 90, 180, 365], metric: (c) => c.activity.longestStreak, nameKey: `${KEY}.card.streaker.name`, scopeKey: `${KEY}.scope.longestStreak` },
  { id: 'regular', category: 'activity', icon: 'calendar', unit: 'day', thresholds: [150, 365, 730, 1100, 1500], metric: (c) => c.activity.activeDays, nameKey: `${KEY}.card.regular.name`, scopeKey: `${KEY}.scope.activeDays` },
  { id: 'nightowl', category: 'activity', icon: 'moon', unit: 'count', thresholds: [50, 200, 500, 1000, 2000], metric: (c) => c.activity.nightUnlocks, nameKey: `${KEY}.card.nightowl.name`, scopeKey: `${KEY}.scope.nightUnlocks` },

  // ── 偏好 ───────────────────────────────────────────────
  { id: 'explorer', category: 'taste', icon: 'globe', unit: 'count', thresholds: [4, 6, 9, 11, 13], metric: (c) => c.taste.genres.length, nameKey: `${KEY}.card.explorer.name`, scopeKey: `${KEY}.scope.genreBreadth` },

  // ── 库藏 ───────────────────────────────────────────────
  { id: 'curator', category: 'library', icon: 'wallet', unit: 'fen', thresholds: [100000, 600000, 1500000, 3000000, 6000000], metric: (c) => c.library.valueFen, nameKey: `${KEY}.card.curator.name`, scopeKey: `${KEY}.scope.libraryValue` },
  { id: 'thrifty', category: 'library', icon: 'pause', unit: 'fenPerHour', thresholds: [6000, 3000, 1500, 800, 400], inverse: true, metric: (c) => (c.playtime.totalMin >= 6000 ? c.library.costPerHourFen : 100000), nameKey: `${KEY}.card.thrifty.name`, scopeKey: `${KEY}.scope.costPerHour` },
  { id: 'shelf', category: 'library', icon: 'list', unit: 'count', thresholds: [50, 120, 250, 400, 600], metric: (c) => c.playtime.playedGames, nameKey: `${KEY}.card.shelf.name`, scopeKey: `${KEY}.scope.playedGames` },
]

/** 评测一张名片：五级阶梯的阶位与级内进度 */
export function evaluateTitleCard(spec: CareerCardSpec, c: CareerPayload): CareerTitleCard {
  const raw = spec.metric(c)
  const value = Number.isFinite(raw) ? raw : 0
  if (spec.inverse) {
    let level = 0
    for (const t of spec.thresholds) {
      if (value > 0 && value <= t) level += 1
      else break
    }
    const maxed = level >= 5
    const base = level > 0 ? spec.thresholds[level - 1] : spec.thresholds[0] * 2
    const target = maxed ? null : spec.thresholds[level]
    // inverse 的级内进度：从上一阶阈值继续压到下一阶（value 越小越满）
    const progress = maxed || target === null ? 1 : Math.min(1, Math.max(0, (base - value) / (base - target)))
    return { ...spec, value, level, progress, target, maxed, tier: cardTierOf(level) }
  }
  let level = 0
  for (const t of spec.thresholds) {
    if (value >= t) level += 1
    else break
  }
  const maxed = level >= 5
  const base = level > 0 ? spec.thresholds[level - 1] : 0
  const target = maxed ? null : spec.thresholds[level]
  const progress = maxed || target === null ? 1 : Math.min(1, Math.max(0, (value - base) / (target - base)))
  return { ...spec, value, level, progress, target, maxed, tier: cardTierOf(level) }
}

/** 全量评测：按「阶位高 → 级内进度高 → id」排序，名片墙头部永远是最硬的卡 */
export function evaluateTitleCards(c: CareerPayload): CareerTitleCard[] {
  return TITLE_CARDS.map((s) => evaluateTitleCard(s, c)).sort((a, b) => {
    if (a.level !== b.level) return b.level - a.level
    if (a.progress !== b.progress) return b.progress - a.progress
    return a.id.localeCompare(b.id)
  })
}

/* ── 生涯评分（五级阶梯 + 权重环）───────────────────────── */

export interface CareerScoreDim {
  id: string
  key: MessageKey
  /** 数值单位：决定取整与单位词（hour/day/count/fen） */
  unit: 'hour' | 'day' | 'count' | 'fen'
  value: number
  /** 本维权重（总评环的连续弧长按权重合成） */
  weight: number
  /** 当前所在级 1..5；0 = 连第一级都未达成 */
  level: number
  /** 级内进度 0~1；满级恒 1 */
  progress: number
  /** 本级区间下界（一级从 0 起算） */
  base: number
  /** 本级目标（= 下一级阈值）；满级为 null */
  target: number | null
  maxed: boolean
}

export interface CareerScore {
  /** 总阶 = 五维最低阶：五维全达同一阶才算升上去 */
  level: number
  /** 环弧进度 0~1：各维「五阶全程进度」按权重合成（连续值，非分段） */
  weighted: number
  maxed: boolean
  dims: CareerScoreDim[]
}

/** 五维阶梯表：每一维锚在**不同**的数据族上，压交叉相关性——
 *  时长=游玩总量、单作=集中深度、白金=完成追求、活跃=登录习惯、库值=钱包投入
 *  （稀有度量与口味广度不进评分，稀有度量由称号名片承载）。
 *  阈值方向：白金 20 款只是一阶满条件；时长一级 2000、二级 3200；
 *  五级留给殿堂账号。 */
export const SCORE_LADDERS: readonly {
  id: string
  key: MessageKey
  unit: CareerScoreDim['unit']
  weight: number
  thresholds: readonly [number, number, number, number, number]
  metric: (c: CareerPayload) => number
}[] = [
  { id: 'time', key: `${KEY}.score.time`, unit: 'hour', weight: 0.24, thresholds: [2000, 3200, 5000, 7000, 10000], metric: (c) => c.playtime.totalMin / 60 },
  { id: 'depth', key: `${KEY}.score.depth`, unit: 'hour', weight: 0.16, thresholds: [250, 400, 600, 850, 1200], metric: (c) => c.playtime.maxMin / 60 },
  { id: 'perfect', key: `${KEY}.score.perfect`, unit: 'count', weight: 0.22, thresholds: [20, 50, 100, 180, 300], metric: (c) => c.trophy.perfect },
  { id: 'active', key: `${KEY}.score.active`, unit: 'day', weight: 0.18, thresholds: [200, 365, 550, 730, 1000], metric: (c) => c.activity.activeDays },
  { id: 'library', key: `${KEY}.score.library`, unit: 'fen', weight: 0.2, thresholds: [2000000, 6000000, 12000000, 25000000, 50000000], metric: (c) => c.library.valueFen },
]

/** 阶位名（1~5 阶），模块级只存 key，渲染期 t() */
export const SCORE_LEVEL_KEYS: readonly MessageKey[] = [
  `${KEY}.score.lvl1`,
  `${KEY}.score.lvl2`,
  `${KEY}.score.lvl3`,
  `${KEY}.score.lvl4`,
  `${KEY}.score.lvl5`,
]

/** 五级阶梯评分：总阶取五维最小值（五维全达同阶才升）；环弧 = 各维
 *  「五阶全程进度」（已过阶数 + 级内进度，除以 5）按权重合成的连续值——
 *  单弧连续生长，不做机械分段。 */
export function careerScore(c: CareerPayload): CareerScore {
  const dims: CareerScoreDim[] = SCORE_LADDERS.map((ladder) => {
    const raw = ladder.metric(c)
    const value = Number.isFinite(raw) ? raw : 0
    let level = 0
    for (const t of ladder.thresholds) {
      if (value >= t) level += 1
      else break
    }
    const maxed = level >= 5
    const base = level > 0 ? ladder.thresholds[level - 1] : 0
    const target = maxed ? null : ladder.thresholds[level]
    const progress = maxed || target === null ? 1 : (value - base) / (target - base)
    return {
      id: ladder.id,
      key: ladder.key,
      unit: ladder.unit,
      weight: ladder.weight,
      value,
      level,
      progress: Math.min(1, Math.max(0, progress)),
      base,
      target,
      maxed,
    }
  })
  const level = dims.reduce((min, d) => Math.min(min, d.level), 5)
  const weighted = dims.reduce((sum, d) => sum + d.weight * ((d.level + d.progress) / 5), 0)
  return { level, weighted: Math.min(1, weighted), maxed: level >= 5, dims }
}

/** 卡片上的数值格式化：单位文案走词典，数值本身在这里定精度 */
export function formatMetric(
  value: number,
  unit: CareerMetricUnit,
  t: (key: MessageKey, params?: Record<string, string | number>) => string,
): string {
  switch (unit) {
    case 'hour':
      return t(`${KEY}.unit.hour`, { n: value >= 100 ? Math.round(value) : value.toFixed(1) })
    case 'min':
      return t(`${KEY}.unit.min`, { n: Math.round(value) })
    case 'percent':
      return t(`${KEY}.unit.percent`, { n: value.toFixed(1) })
    case 'fen':
      return t(`${KEY}.unit.fen`, { n: (value / 100).toFixed(2) })
    case 'fenPerHour':
      return t(`${KEY}.unit.fenPerHour`, { n: (value / 100).toFixed(2) })
    case 'perHour':
      return t(`${KEY}.unit.perHour`, { n: value.toFixed(2) })
    case 'day':
      return t(`${KEY}.unit.day`, { n: Math.round(value) })
    default:
      return t(`${KEY}.unit.count`, { n: Math.round(value) })
  }
}
