/** 游戏生涯模块的展示层小工具：伪随机扫光相位、数值/时长/日期格式化。
 *
 *  扫光相位放这里而不是各组件内联：同一个「珍稀物件掠过一道光」的效果在
 *  称号墙、陈列墙、纪录卡、箴言墙上都要用，相位算法抄四遍必然四份不一样。
 *  数值格式化同理——格式化的**精度**在这里定，**单位文案**一律走词典
 *  （模块级常量不许存译文）。
 */

import type { MessageKey } from '@/locales'

/** 视图侧 t() 的最小签名（与 locales/index.ts 的 useI18n().t 一致） */
export type TFn = (key: MessageKey, params?: Record<string, string | number>) => string

/** 按种子取伪随机扫光相位（时长 4.1~6.5s，负延迟错开首帧）。
 *
 *  乘法哈希而非直接取模：appid 大量以 0 结尾，直接取模会退化成同一值，
 *  先散列再取模，时长与相位才真正彼此错开。 */
export function sheenStyle(seed: number): Record<string, string> {
  const h = Math.abs((seed * 2654435761) % 997)
  return {
    '--cr-sheen-duration': `${(4.1 + (h % 5) * 0.47).toFixed(2)}s`,
    '--cr-sheen-delay': `${-((h % 17) * 0.37).toFixed(2)}s`,
  }
}

/** 分钟 → 人类可读时长（< 10h 保留一位小数，≥ 1000h 转千小时） */
export function fmtHours(minutes: number, t: TFn): string {
  if (!minutes || minutes <= 0) return t('common.hours', { h: 0 })
  const h = minutes / 60
  if (h < 10) return t('common.hours', { h: h.toFixed(1) })
  if (h < 1000) return t('common.hours', { h: h.toFixed(0) })
  return t('common.hoursK', { h: (h / 1000).toFixed(1) })
}

/** 秒级时间戳 → 本地日期串；0 / 空值返回空串（兜底文案由调用方给） */
export function fmtDate(ts: number): string {
  return ts > 0 ? new Date(ts * 1000).toLocaleDateString() : ''
}

/** 分 → 紧凑时长：< 1 小时给分钟，< 48 小时给小时，再往上给天 */
export function fmtDuration(minutes: number, t: TFn): string {
  if (!minutes || minutes <= 0) return t('achievements.career.title.unit.min', { n: 0 })
  if (minutes < 60) return t('achievements.career.title.unit.min', { n: Math.round(minutes) })
  if (minutes < 60 * 48) {
    return t('achievements.career.title.unit.hour', { n: Math.round(minutes / 60) })
  }
  return t('achievements.career.heat.stat.days', { n: Math.round(minutes / 60 / 24) })
}

/** 万分比（0~10000）→ 百分数数值（保留一位小数） */
export function toPercent(rate: number): number {
  return Math.round((rate / 100) * 10) / 10
}