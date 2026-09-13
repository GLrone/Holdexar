import { defineStore } from 'pinia'
import { ref } from 'vue'
import { APP_SLUG } from '@/appInfo'

export type Locale = 'zh-CN' | 'en'

const STORAGE_KEY = `${APP_SLUG}.locale`

/**
 * 首次访问的默认语言：跟随浏览器。
 *
 * 此前硬编码 `'zh-CN'`，非中文用户第一次打开就是满屏中文（他们是唯一需要
 * 「默认英文」的人群，而恰恰只有他们会被这个默认值挡住）。只在**没有持久化
 * 选择**时才走这里——用户点过语言开关之后，他的选择永远优先于系统语言。
 *
 * 判据用 `startsWith('zh')` 而不是 `=== 'zh-CN'`：zh-TW / zh-HK / zh-SG 的
 * 用户看简体也远好过看英文，而本项目只有简中一份中文词典，没有分流的意义。
 * 读不到 `navigator`（非浏览器环境 / SSR / 测试）时保守回中文。
 */
function detectLocale(): Locale {
  try {
    const langs = navigator.languages?.length ? navigator.languages : [navigator.language]
    for (const l of langs) {
      if (!l) continue
      const low = l.toLowerCase()
      if (low.startsWith('zh')) return 'zh-CN'
      // 只认明确给出的语言，避免把 undefined/空串当英文
      if (low.startsWith('en')) return 'en'
    }
  } catch {
    /* navigator 不可用：落回中文 */
  }
  return 'zh-CN'
}

/** 界面语言状态：切换 html.lang + localStorage 持久化（词典与 t() 见 @/locales）。 */
export const useLocaleStore = defineStore('locale', () => {
  const stored = localStorage.getItem(STORAGE_KEY)
  // 取出的值必须是已知语言：改过 STORAGE_KEY 后缀、或用户手改过 localStorage 时，
  // 脏值会让 t() 查不到词典、整屏回退成 key 原样（比语言选错更难排查）。
  const locale = ref<Locale>(stored === 'en' || stored === 'zh-CN' ? stored : detectLocale())

  /**
   * 同步到 DOM。**刻意不写 localStorage**：只有用户显式选过语言才持久化。
   * 探测出来的语言若立刻落盘，就等于把「系统是英文」永久固化成「用户选了英文」——
   * 之后用户把系统改成中文，界面却再也不跟了，且没有任何地方能看出原因。
   * 现在的语义是「没选过 → 一直跟随系统；选过 → 你的选择永远优先」。
   */
  function apply() {
    document.documentElement.lang = locale.value
  }

  function set(next: Locale) {
    locale.value = next
    apply()
    localStorage.setItem(STORAGE_KEY, next)
  }

  function toggle() {
    set(locale.value === 'zh-CN' ? 'en' : 'zh-CN')
  }

  // 初始化即应用（main.ts 挂载前也会调一次，双保险，同 theme store）
  apply()

  return { locale, set, toggle, apply }
})
