/* ════════════════════════════════════════════════════════════════════
   轻量 i18n 出口：useI18n() 返回 t(key, params)。
   响应式来源 = locale store（t 在渲染期读 store.locale，切换即全局重渲染）。
   缺译回退：当前语言 → zh-CN → key 原样。
   词典按模块拆在 `zh-CN/` 与 `en/` 下（见各自 index.ts 的约定说明）。
   数字/日期本地化走同目录的 format.ts，勿再写死 toLocaleString('zh-CN')。
   ════════════════════════════════════════════════════════════════════ */

import zhCN, { type MessageKey } from './zh-CN'
import en from './en'
import { useLocaleStore } from '@/stores/locale'
import type { Locale } from '@/stores/locale'

export { useLocaleFormat, type LocaleFormat } from './format'
export type { MessageKey }

const DICTS: Partial<Record<Locale, Partial<Record<MessageKey, string>>>> = {
  'zh-CN': zhCN,
  en,
}

export function useI18n() {
  const store = useLocaleStore()

  function t(key: MessageKey, params?: Record<string, string | number>): string {
    let text: string = DICTS[store.locale]?.[key] ?? zhCN[key] ?? key
    if (params) {
      for (const [k, v] of Object.entries(params)) {
        text = text.replaceAll(`{${k}}`, String(v))
      }
    }
    return text
  }

  return { t, locale: store.locale }
}
