/**
 * 币种元数据：**结构**（代号 + 国旗所属地区码）与查表出口。
 *
 * 这里**没有币种名**——名字在词典里（`locales/zh-CN/currencies.ts` 的
 * `currencies.name.<CODE>`）：中文字面量写在这里会让本文件无法通过 eslint 的
 * CJK 规则（它整目录豁免 `src/locales/**`，却如实扫 `api/**`）。取名字一律走
 * `currencyName()`。
 *
 * 单一来源对齐 `server/app/crawler/config.py` 的 CC_LIST（41 区 → 37 种唯一货币，
 * 按 CC_LIST 首次出现顺序排列）；末尾 TRY / ARS 为服务端永久保留币种
 * （当前无区服使用，后续拓展业务用）。
 * 新增区服货币时：CC_LIST 加行 + 此处补一条 + 两个词典各补一条名字。
 */
import { useI18n, type MessageKey } from '@/locales'

import { flagUrl } from './regions'

export interface CurrencyMeta {
  /** ISO 代号（USD / JPY …） */
  code: string
  /** 国旗所属地区码（flagUrl 用） */
  cc: string
}

export const CURRENCIES: CurrencyMeta[] = [
  { code: 'CNY', cc: 'cn' },
  { code: 'RUB', cc: 'ru' },
  { code: 'KZT', cc: 'kz' },
  { code: 'UAH', cc: 'ua' },
  { code: 'USD', cc: 'us' },
  { code: 'VND', cc: 'vn' },
  { code: 'IDR', cc: 'id' },
  { code: 'INR', cc: 'in' },
  { code: 'BRL', cc: 'br' },
  { code: 'CLP', cc: 'cl' },
  { code: 'JPY', cc: 'jp' },
  { code: 'HKD', cc: 'hk' },
  { code: 'PHP', cc: 'ph' },
  { code: 'TWD', cc: 'tw' },
  { code: 'KWD', cc: 'kw' },
  { code: 'SAR', cc: 'sa' },
  { code: 'ZAR', cc: 'za' },
  { code: 'QAR', cc: 'qa' },
  { code: 'MYR', cc: 'my' },
  { code: 'THB', cc: 'th' },
  { code: 'PEN', cc: 'pe' },
  { code: 'MXN', cc: 'mx' },
  { code: 'SGD', cc: 'sg' },
  { code: 'AED', cc: 'ae' },
  { code: 'UYU', cc: 'uy' },
  { code: 'COP', cc: 'co' },
  { code: 'KRW', cc: 'kr' },
  { code: 'NZD', cc: 'nz' },
  { code: 'PLN', cc: 'pl' },
  { code: 'CRC', cc: 'cr' },
  { code: 'CAD', cc: 'ca' },
  { code: 'AUD', cc: 'au' },
  { code: 'EUR', cc: 'eu' },
  { code: 'GBP', cc: 'gb' },
  { code: 'NOK', cc: 'no' },
  { code: 'ILS', cc: 'il' },
  { code: 'CHF', cc: 'ch' },
  // ── 预留币种（当前无区服使用，服务端仍抓取落库） ──
  { code: 'TRY', cc: 'tr' },
  { code: 'ARS', cc: 'ar' },
]

const BY_CODE = new Map(CURRENCIES.map((c) => [c.code, c]))

/**
 * 币种名（当前语言；未知币种回退代号本身）。
 *
 * **每次调用现读语言**（`t()` 内部读 locale store），因此在 `computed`、模板
 * 渲染表达式与普通函数里都是响应式的——切语言即重算，不需要额外 watch。
 * 这条性质是必要的：调用点里有 plain function（`api/selectOptions.ts` 的
 * `currencySelectOptions`），拿不到组件上下文，靠的就是「t() 在调用期读 store」。
 *
 * **必须保留「任何输入都返回字符串、绝不抛」**——这是它从 `currencyZh` 继承的
 * 契约，调用点全都不做 try/catch。为此这里先过 `BY_CODE` 这道已知集闸门：
 * 只有表内的 39 个代号才会去构造词典 key，未知输入直接回退代号本身。
 * 不去调 `Intl.DisplayNames` 现取，理由有两条：
 *   ① zh 侧 Intl 与项目既有说法不一致（IDR / ILS 两条，见词典头注）；
 *   ② en 侧 Intl 的容忍度不可依赖——币种类型接受小写（`of('usd')` 正常返回
 *      "US Dollar"）而区域类型把小写原样回显（`of('cn')` → `"cn"`）、未知但
 *      合形的码在区域类型上直接抛 RangeError。词典没有这些脾气。
 *
 * 查表**大小写敏感**，与同文件的 `currencyFlagUrl` / `currencyIndex` 一致
 * （代号在本项目里恒为大写：表内字面量、`GET /rates` 的 `currency` 字段、
 * 下拉 value 三处来源都是大写）。
 */
export function currencyName(code: string): string {
  const meta = BY_CODE.get(code)
  return meta ? useI18n().t(`currencies.name.${meta.code}` as MessageKey) : code
}

/** 国旗地址（未知币种返回空串，调用方隐藏 img） */
export function currencyFlagUrl(code: string): string {
  const cc = BY_CODE.get(code)?.cc
  return cc ? flagUrl(cc) : ''
}

/** 白名单序号（未知币种排最后），列表展示排序用 */
export function currencyIndex(code: string): number {
  const idx = CURRENCIES.findIndex((c) => c.code === code)
  return idx === -1 ? CURRENCIES.length : idx
}

// ── 本币金额格式化（换算气泡 / 悬停提示用）─────────────────────
//
// 与 `server/app/domains/games/pricing.py` 的 CURRENCY_SYMBOLS /
// DISPLAY_DECIMALS / _SUFFIX_CURRENCIES 保持同构：前端只做「人民币分换算出
// 的本币分 → 展示文本」这一步，字段源头以服务端为准。两边新增币种时同步改。

const CURRENCY_SYMBOLS: Record<string, string> = {
  CNY: '¥', RUB: '₽', KZT: '₸', UAH: '₴', USD: '$', VND: '₫', IDR: 'Rp',
  INR: '₹', BRL: 'R$', CLP: '$', JPY: '¥', HKD: 'HK$', PHP: '₱', TWD: 'NT$',
  KWD: 'KD', SAR: 'SR', ZAR: 'R', QAR: 'QR', MYR: 'RM', THB: '฿', PEN: 'S/',
  MXN: 'MX$', SGD: 'S$', AED: 'AED', UYU: '$U', COP: 'COL$', KRW: '₩',
  NZD: 'NZ$', PLN: 'zł', CRC: '₡', CAD: 'C$', AUD: 'A$', EUR: '€', GBP: '£',
  NOK: 'kr', ILS: '₪', CHF: 'CHF',
}

/** 零小数货币（Steam 以整数计价，存分后显示回整数） */
const ZERO_DECIMAL_CURRENCIES = new Set(['CLP', 'VND', 'IDR', 'JPY', 'KZT', 'UAH'])

/** 符号后置的货币（如 "55.00 ₽" / "119,00 zł"） */
const SUFFIX_CURRENCIES = new Set(['RUB', 'KZT', 'UAH', 'PLN', 'NOK'])

/**
 * 本币最小单位 → 展示文本（如 699 + USD → "$6.99"）。
 * 与服务端 format_minor_units 同构：千分位逗号、零小数币种取整、后置符号。
 */
export function formatMinor(amountMinor: number | null | undefined, currency: string): string {
  if (amountMinor === null || amountMinor === undefined || !Number.isFinite(amountMinor)) {
    return ''
  }
  const code = currency.toUpperCase()
  const symbol = CURRENCY_SYMBOLS[code] ?? ''
  const major = amountMinor / 100
  const num = ZERO_DECIMAL_CURRENCIES.has(code)
    ? Math.round(major).toLocaleString('en-US')
    : major.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  return SUFFIX_CURRENCIES.has(code) ? `${num} ${symbol}`.trim() : `${symbol}${num}`
}
