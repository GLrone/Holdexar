import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { settingsApi } from '@/api/client'
import { APP_SLUG } from '@/appInfo'

export type ThemeMode = 'dark' | 'light'

const STORAGE_KEY = `${APP_SLUG}.theme`

export const useThemeStore = defineStore('theme', () => {
  const theme = ref<ThemeMode>(
    localStorage.getItem(STORAGE_KEY) === 'light' ? 'light' : 'dark',
  )
  const isDark = computed(() => theme.value === 'dark')
  let changed = false
  let hasSaved = false
  let revision = 0
  let confirmedTheme = theme.value
  let saveTail: Promise<unknown> = Promise.resolve()

  function apply() {
    document.documentElement.classList.toggle('dark', isDark.value)
    document.documentElement.style.colorScheme = isDark.value ? 'dark' : 'light'
    localStorage.setItem(STORAGE_KEY, isDark.value ? 'dark' : 'light')
  }

  function hydrate(value: ThemeMode) {
    if (value !== 'dark' && value !== 'light') return
    if (!hasSaved) confirmedTheme = value
    if (changed) return
    theme.value = value
    apply()
  }

  async function toggle(): Promise<boolean> {
    changed = true
    const current = ++revision
    const next = theme.value === 'dark' ? 'light' : 'dark'
    theme.value = next
    apply()
    const saving = saveTail.then(() => settingsApi.update({ theme: next }))
    saveTail = saving.catch(() => {})
    try {
      const saved = await saving
      hasSaved = true
      confirmedTheme = saved.theme
      return true
    } catch {
      if (current === revision) {
        if (!hasSaved) changed = false
        theme.value = confirmedTheme
        apply()
      }
      return false
    }
  }

  apply()

  return { theme, isDark, toggle, apply, hydrate }
})
