/**
 * forceReload.ts —— 全局强制刷新快捷键（桌面终端无浏览器刷新键可用）。
 *
 * 拦截 F5 / Ctrl+R / Ctrl+Shift+R / Ctrl+F5（macOS 为 ⌘R），
 * 先以 cache: 'reload' 重取当前路径（强制更新 HTTP 缓存里的 index.html，
 * 否则新构建的产物名变了也拿不到），再 location.reload()。
 * capture 阶段注册，先于 WebView2 内建加速键与页面内按键处理。
 */

/** 强制刷新当前页（绕过 index.html 的 HTTP 缓存） */
export async function forceReloadPage(): Promise<void> {
  try {
    await fetch(location.href, { cache: "reload" })
  } catch {
    /* 后端不可达时照常 reload，让页面呈现错误态而非无响应 */
  }
  location.reload()
}

export function setupForceReload(): void {
  window.addEventListener(
    "keydown",
    (e: KeyboardEvent) => {
      const isF5 = e.key === "F5"
      const isReloadCombo =
        (e.ctrlKey || e.metaKey) && (e.key === "r" || e.key === "R")
      if (!isF5 && !isReloadCombo) return
      e.preventDefault()
      e.stopPropagation()
      void forceReloadPage()
    },
    { capture: true },
  )
}
