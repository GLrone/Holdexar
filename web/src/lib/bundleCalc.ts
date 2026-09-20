/**
 * bundleCalc.ts — 捆绑包补齐计算引擎
 *
 * - inferBundleOwnership 撞库推演（condition B：基准 appids 全部已拥有/家庭共享）
 * - calculateBundlePrice 补齐计算（排除后逐游戏求和 × 整包基础折扣）
 *
 * 行为口径说明：
 * 1. 无 sub 归属数据，不做 condition A（ownedPackages 确诊已购）；
 *    condition B 覆盖其主场景。
 * 2. 游戏现价库内既有外币 minor 原价又有 cny_fen，两币种各自
 *    直接求和，不做外币经汇率逆推（口径更准，判断结论一致）。
 */
import type { BundleDetail, BundleGame, BundleRegionPrice } from '@/api/client'
import type { OwnershipInfo } from '@/api/client'

// ── 外币展示（后端 games/pricing.py 同款数据表的镜像） ──

const CURRENCY_SYMBOLS: Record<string, string> = {
  CNY: '¥', RUB: '₽', KZT: '₸', UAH: '₴', USD: '$', VND: '₫', IDR: 'Rp',
  INR: '₹', BRL: 'R$', CLP: '$', JPY: '¥', HKD: 'HK$', PHP: '₱', TWD: 'NT$',
  KWD: 'KD', SAR: 'SR', ZAR: 'R', QAR: 'QR', MYR: 'RM', THB: '฿', PEN: 'S/',
  MXN: 'MX$', SGD: 'S$', AED: 'AED', UYU: '$U', COP: 'COL$', KRW: '₩',
  NZD: 'NZ$', PLN: 'zł', CRC: '₡', CAD: 'C$', AUD: 'A$', EUR: '€', GBP: '£',
  NOK: 'kr', ILS: '₪', CHF: 'CHF',
}

const SUFFIX_CURRENCIES = new Set(['KZT', 'UAH', 'VND', 'NOK', 'ILS', 'PLN'])

/** Steam 无小数展示的币种（minor 单位即主单位） */
const ZERO_DECIMALS = new Set(['CLP', 'VND', 'IDR', 'JPY', 'KZT', 'UAH'])

export function formatForeignMinor(amountMinor: number | null, currency: string | null): string {
  if (amountMinor == null) return ''
  const cur = (currency || '').toUpperCase()
  const symbol = CURRENCY_SYMBOLS[cur] ?? ''
  const major = amountMinor / 100
  const formatted = ZERO_DECIMALS.has(cur)
    ? Math.round(major).toLocaleString('en-US')
    : major.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  return SUFFIX_CURRENCIES.has(cur) ? `${formatted} ${symbol}`.trim() : `${symbol}${formatted}`
}

// ── 撞库推演 ──

export interface BundleOwnership {
  type: 'owned' | 'family' | null
  /** family 时的归属账户显示名 */
  account: string | null
}

/**
 * 撞库推演：基准 appids 里的"有效游戏"（库内存在 = name 非空的条目）
 * 全部已拥有 → owned；全部靠家庭共享 → family(account)；混编 → owned。
 * 库内无任何有效游戏时不判定（至少一个有效游戏才进入判定）。
 */
export function inferBundleOwnership(
  appIds: number[],
  games: BundleGame[] | null,
  ownership: Record<number, OwnershipInfo>,
): BundleOwnership {
  const valid = (games ?? []).filter((g) => g.name).map((g) => g.appid)
  if (appIds.length === 0 || valid.length === 0) return { type: null, account: null }

  let allOwnedOrFamily = true
  let isPureFamily = true
  let anyFamilyAccount: string | null = null
  for (const appid of valid) {
    const info = ownership[appid]
    const isSelf = info?.type === 'owned'
    const isFamily = info?.type === 'family'
    if (!isSelf && !isFamily) {
      allOwnedOrFamily = false
      break
    }
    if (isSelf) {
      isPureFamily = false
    } else if (isFamily && !anyFamilyAccount) {
      const owner = info.owners?.[0]
      if (owner) anyFamilyAccount = owner
    }
  }
  if (!allOwnedOrFamily) return { type: null, account: null }
  if (isPureFamily && anyFamilyAccount) return { type: 'family', account: anyFamilyAccount }
  // `account` 与上面两支一样给 null：**owned 分支的 account 从来不被读**。
  // 唯一的消费点 bundles/Index.vue 是三元的非 owned 支（`type === 'owned' ? … :
  // [account ?? '']`），owned 时走的是另一支，显示的是 `bundles.badge.owned`。
  // 这里不放中文字面量——它不会被显示，却会让本文件命中 no-hardcoded-cjk。
  return { type: 'owned', account: null }
}

// ── 补齐计算 ──

export interface CompletionResult {
  /** 官方整包价（未排除任何游戏时）或排除后合计（分） */
  cnyFen: number | null
  /** 外币合计（minor 单位，未排除任何游戏时 = 官方原价） */
  foreignMinor: number | null
  /** 求和命中的有效商品数 */
  validCount: number
  /** 是否官方整包价分支 */
  official: boolean
}

/**
 * 补齐计算：
 * - 未排除任何游戏 → 直接用该区官方整包价（原价与 CNY 均来自区域价行）；
 * - 有排除 → 对未排除游戏在该区的现价求和（外币 minor 与 cny_fen 各自独立求和，
 *   无价游戏跳过不计 validCount），再乘 (1 − 整包基础折扣%)。
 */
export function computeCompletion(
  bundle: Pick<BundleDetail, 'appIds' | 'regionPrices' | 'games'>,
  regionCode: string,
  excluded: ReadonlySet<number>,
): CompletionResult {
  const key = regionCode.toUpperCase()
  const rp: BundleRegionPrice | undefined = bundle.regionPrices[key]
  if (!rp) return { cnyFen: null, foreignMinor: null, validCount: 0, official: false }

  if (excluded.size === 0) {
    return {
      cnyFen: rp.cnyFen,
      foreignMinor: rp.priceMinor,
      validCount: 1,
      official: true,
    }
  }

  const regionAids = new Set(rp.appIds.map(Number))
  let sumMinor = 0
  let sumFen = 0
  let validCount = 0
  for (const game of bundle.games ?? []) {
    if (excluded.has(game.appid) || !regionAids.has(game.appid)) continue
    const price = game.prices[key]
    if (!price) continue
    if (price.priceMinor != null) sumMinor += price.priceMinor
    if (price.cnyFen != null) {
      sumFen += price.cnyFen
      validCount++
    }
  }

  if (validCount === 0) return { cnyFen: null, foreignMinor: null, validCount: 0, official: false }

  const factor = 1 - (rp.baseDiscount || 0) / 100
  return {
    cnyFen: Math.round(sumFen * factor),
    foreignMinor: sumMinor > 0 ? Math.round(sumMinor * factor) : null,
    validCount,
    official: false,
  }
}

/**
 * 计算器自动排除（预选规则）：
 * 库内无数据的游戏（name 为 null）+ 主账户已拥有的游戏。
 * 家庭共享不算拥有（可玩但仍需购买）。
 */
export function autoExcludedAppIds(
  bundle: Pick<BundleDetail, 'appIds' | 'games'>,
  ownership: Record<number, OwnershipInfo>,
): Set<number> {
  const excluded = new Set<number>()
  for (const game of bundle.games ?? []) {
    if (!game.name || ownership[game.appid]?.type === 'owned') excluded.add(game.appid)
  }
  return excluded
}

/**
 * 全区价格展示顺序：CN 固定第一，其余按服务端区服表顺序，未知区码垫底。
 */
export function orderedRegionCodes(
  codes: string[],
  knownOrder: { code: string }[],
): string[] {
  const upper = [...new Set(codes.map((c) => c.toUpperCase()))]
  const known = knownOrder.map((r) => r.code.toUpperCase())
  const head = known.filter((c) => upper.includes(c) && c !== 'CN')
  const tail = upper.filter((c) => !known.includes(c)).sort()
  return ['CN', ...head, ...tail].filter((c) => upper.includes(c))
}
