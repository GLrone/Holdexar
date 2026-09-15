import { defineStore } from 'pinia'
import { ref, watch } from 'vue'
import { APP_SLUG } from '@/appInfo'

export type ThemeMode = 'dark' | 'light'

const STORAGE_KEY = `${APP_SLUG}.theme`

/** 主题状态：切换 html.dark + localStorage 持久化 + logo 联动。 */
export const useThemeStore = defineStore('theme', () => {
  const theme = ref<ThemeMode>(
    (localStorage.getItem(STORAGE_KEY) as ThemeMode) || 'dark',
  )

  const isDark = ref(theme.value === 'dark')

  function apply() {
    document.documentElement.classList.toggle('dark', isDark.value)
    document.documentElement.style.colorScheme = isDark.value ? 'dark' : 'light'
    localStorage.setItem(STORAGE_KEY, isDark.value ? 'dark' : 'light')
    // 主题镜像到本地库（app_settings.ui.theme）：关闭弹窗是独立 WinForms
    // 窗，读不到 localStorage，靠这份镜像跟随主题（初始化 + 每次切换都会
    // 走到这里，镜像始终新鲜）。fire-and-forget：失败静默——镜像缺位只
    // 影响弹窗配色，无碍功能。
    void fetch('/api/v1/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ theme: isDark.value ? 'dark' : 'light' }),
    }).catch(() => {})
  }

  function toggle() {
    isDark.value = !isDark.value
    theme.value = isDark.value ? 'dark' : 'light'
    apply()
  }

  // 初始化即应用（main.ts 挂载前也会调一次，双保险）
  apply()

  return { theme, isDark, toggle, apply }
})
