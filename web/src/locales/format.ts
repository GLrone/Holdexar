/* ════════════════════════════════════════════════════════════════════
   数字 / 日期 / 时间的本地化格式出口。

   **为什么需要它**：全项目有 20 处直接写死 `toLocaleString('zh-CN')` /
   `toLocaleTimeString('zh-CN')`。这类字面量散落在 views/ 各处，逐个替换没有
   公共落点就会再次漂移回硬编码。

   **别把它当成一个已发生的 bug 修复——实测过，zh-CN 与 en-US 的数字输出逐字
   相同**：`1,234,567.891` / `1,234.50` / `1,234.57` 两种语言下完全一致（分组符
   与小数点同形）。当前**真正**会变的只有时间：`14:30` → `02:30 PM`。
   所以这里买到的不是「修好了英文界面的数字」，而是三件更长期的事：
   ① 时间/日期这类确实有分歧的格式有了正确落点；
   ② 将来若加入 de-DE / hi-IN 这类分组规则不同的语言，不必再回头逐处改；
   ③ 20 处硬编码收敛成一个可被门禁扫描的出口。

   **两种调用形态都有意支持**：
   - `const fmt = useLocaleFormat()` 取一组函数（模板/脚本里用）
   - 函数体内**每次调用现读** `locale.value`，因此在 `computed`、模板渲染
     表达式里使用时会自动建立依赖——切语言即重算，不需要额外 watch。

   **格式化器缓存**：`Intl.*Format` 的构造开销远大于格式化本身，而列表里
   逐行调用会构造成百上千次。按 (语言, 选项) 记忆，模块级存活。
   ════════════════════════════════════════════════════════════════════ */

import { useLocaleStore, type Locale } from '@/stores/locale'

/** 语言 → BCP-47 标记。`en` 走 `en-US`：日期顺序与 12 小时制取美式习惯 */
const TAGS: Record<Locale, string> = {
  'zh-CN': 'zh-CN',
  en: 'en-US',
}

type NumOpts = Intl.NumberFormatOptions
type DateOpts = Intl.DateTimeFormatOptions

const numCache = new Map<string, Intl.NumberFormat>()
const dateCache = new Map<string, Intl.DateTimeFormat>()

function num(tag: string, opts: NumOpts): Intl.NumberFormat {
  const key = `${tag}|${JSON.stringify(opts)}`
  let f = numCache.get(key)
  if (!f) {
    f = new Intl.NumberFormat(tag, opts)
    numCache.set(key, f)
  }
  return f
}

function date(tag: string, opts: DateOpts): Intl.DateTimeFormat {
  const key = `${tag}|${JSON.stringify(opts)}`
  let f = dateCache.get(key)
  if (!f) {
    f = new Intl.DateTimeFormat(tag, opts)
    dateCache.set(key, f)
  }
  return f
}

/** 金额固定两位（原 `{ minimumFractionDigits: 2, maximumFractionDigits: 2 }`） */
const MONEY: NumOpts = { minimumFractionDigits: 2, maximumFractionDigits: 2 }
/** 最多两位、不补零（原 `{ maximumFractionDigits: 2 }`） */
const ROUND2: NumOpts = { maximumFractionDigits: 2 }
/** 时:分（原 `{ hour: '2-digit', minute: '2-digit' }`） */
const HHMM: DateOpts = { hour: '2-digit', minute: '2-digit' }

/**
 * 年月 / 月 —— **选项本身随语言变**，这是上面那些常量做不到的。
 *
 * 中文的 `long` 月份是「1月」，英文的 `long` 是 "January"——直接用同一份选项，
 * 英文会得到冗长的 "January 2024"（热力图轴上放不下），中文则永远拿不到
 * 「2024年1月」这个顺序。所以两边各写一份：
 *   zh-CN → `2024年1月` / `1月`
 *   en-US → `Jan 2024` / `Jan`
 *
 * **不提供 `date()`（年月日）**：全项目既有的 YYYY-MM-DD 出自各处手写的
 * 日期截取，中英两种语言下都是同一串 ISO 文本，语言中立，没有分歧要收敛。
 * 加一个没人消费的方法只会变成下一个死 token。
 */
const YEAR_MONTH: Record<Locale, DateOpts> = {
  'zh-CN': { year: 'numeric', month: 'long' },
  en: { year: 'numeric', month: 'short' },
}
const MONTH_ONLY: Record<Locale, DateOpts> = {
  'zh-CN': { month: 'long' },
  en: { month: 'short' },
}

export interface LocaleFormat {
  /** 千分位整数/小数（默认最多 3 位小数）。件数、次数、余额整数部分用 */
  group: (n: number) => string
  /** 金额固定两位：`1,234.56` / `1,234.56`。汇率、账单、钱包用 */
  money: (n: number) => string
  /** 最多两位不补零：`1,234.5`。统计值用 */
  round2: (n: number) => string
  /**
   * 固定小数位：`fixed(2.7, 1)` → `2.7`、`fixed(3, 1)` → `3.0`、`fixed(1234.5, 2)` → `1234.50`。
   *
   * **故意不经 Intl**（直接 `toFixed`），与 `money` / `round2` 的差别有二，都是刻意的：
   *   ① **不补分组符**——`money(1234.5)` 是 `1,234.50`，`fixed(1234.5, 2)` 是 `1234.50`；
   *   ② **位数为必填**——`money` 恒两位、`round2` 最多两位不补零，只有这里能表达
   *      「固定 1 位」（`3.0` 不塌成 `3`）与「固定 0 位」。
   *
   * 为什么需要它：期 6 迁移时多个 family tab 原本写 `(x).toFixed(1)`，因为没有这个出口，
   * 执行者只能改用 `round2`，于是 `3.0` 显示成 `3`、`2.70` 显示成 `2.7`——**显示被改了**，
   * 而 `toFixed` 的输出在 zh-CN / en-US 下逐字相同（见文件头），本来就不是双语缺陷。
   * 这个出口的存在意义就是：让「统一走出口」与「不改显示」两个要求不再互相冲突。
   * 将来若有 de-DE 这类小数点为逗号的语言，只需改这一个函数体。
   */
  fixed: (n: number, digits: number) => string
  /** 时:分。zh-CN → `14:30`，en-US → `02:30 PM` */
  time: (d: Date | number | string) => string
  /** 年月。zh-CN → `2024年1月`，en-US → `Jan 2024` */
  yearMonth: (d: Date | number | string) => string
  /** 仅月份。zh-CN → `1月`，en-US → `Jan`。热力图轴标签用 */
  month: (d: Date | number | string) => string
}

/**
 * 取当前语言的格式化函数组。
 *
 * 返回的函数**在调用时**读取语言，故可安全地放进 `computed` 与模板表达式：
 * 语言一变，用到它的计算属性与渲染都会重新求值。
 */
export function useLocaleFormat(): LocaleFormat {
  const store = useLocaleStore()
  const loc = () => store.locale ?? 'zh-CN'
  const tag = () => TAGS[loc()] ?? TAGS['zh-CN']

  return {
    group: (n) => num(tag(), {}).format(n),
    money: (n) => num(tag(), MONEY).format(n),
    round2: (n) => num(tag(), ROUND2).format(n),
    fixed: (n, digits) => n.toFixed(digits),
    time: (d) => date(tag(), HHMM).format(new Date(d)),
    yearMonth: (d) => date(tag(), YEAR_MONTH[loc()]).format(new Date(d)),
    month: (d) => date(tag(), MONTH_ONLY[loc()]).format(new Date(d)),
  }
}
