/**
 * 钱包余额 CNY 换算（顶栏胶囊 + 余额弹层共用）。
 *
 * 后端 WalletSnapshot.balance 已是主单位（steam_wallet.py 的 /100 规则在
 * 抓取侧完成，无小数货币不做除法）——前端直接 balance × rateToCny 即可。
 * 汇率源 = ratesApi.list()（fx_rates 表，rateToCny 每 CNY 口径）。
 */
import type { RateItem, WalletSnapshot } from '@/api/client'
import { useLocaleFormat } from '@/locales'

/** 汇率表：currency → rate_to_cny（CNY 恒 1.0，rates 服务侧保证） */
export type RateMap = Map<string, number>

export function buildRateMap(rates: RateItem[]): RateMap {
  return new Map(rates.map((r) => [r.currency.toUpperCase(), r.rateToCny]))
}

export interface WalletCny {
  /** CNY 换算值（主单位）；原币即 CNY 或汇率缺失 → null */
  amount: number | null
  /** 原币即 CNY（调用方直接跳过展示换算） */
  isCny: boolean
  /** 汇率缺失（原币非 CNY 且无档案行） */
  missingRate: boolean
}

/**
 * 钱包快照 → CNY 换算信息。
 * - 原币 CNY：isCny=true（调用方跳过）
 * - 非 CNY 无汇率：missingRate=true（显示 —）
 * - 非 CNY 有汇率：amount=balance×rate（两位小数舍入）
 */
export function walletToCny(wallet: WalletSnapshot | null, rates: RateMap): WalletCny {
  if (!wallet) return { amount: null, isCny: false, missingRate: false }
  const code = wallet.currency_code?.toUpperCase()
  if (!code) return { amount: null, isCny: false, missingRate: false }
  if (code === 'CNY') return { amount: null, isCny: true, missingRate: false }
  const rate = rates.get(code)
  if (rate == null) return { amount: null, isCny: false, missingRate: true }
  return { amount: Math.round(wallet.balance * rate * 100) / 100, isCny: false, missingRate: false }
}

/**
 * 换算值展示文本：¥x.xx（null → —；isCny 由调用方跳过根本不调）。
 *
 * 本模块是纯计算 + 一个出口，金额格式走 `useLocaleFormat()` 的 `money`，
 * 使「金额格式」只有这一个可被门禁扫描的落点。`useLocaleStore()` 在函数体内
 * **惰性**取用：pinia 由 `app.use(pinia)` 安装时就 `setActivePinia`，故非 setup
 * 环境（普通模块函数）也能解析到同一个实例；放在模块顶层求值则会在安装前跑、
 * 直接抛错。
 */
export function formatWalletCny(amount: number | null): string {
  if (amount == null) return '—'
  return `¥${useLocaleFormat().money(amount)}`
}
