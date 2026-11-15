import { nextTick, watch, type Ref } from 'vue'

/** 打开中的弹层登记（栈序）：Esc 只关最上层，叠开的弹层不被一次按键全关 */
const openOverlays: Array<{ close: () => void }> = []

/** 弹层无障碍共用（HlDialog / HlDrawer 模态）：Esc 关闭、打开时焦点入面板、
 *  关闭时焦点归还触发元素；role/aria 由调用方模板自理。 */
export function useOverlayA11y(
  model: Ref<boolean>,
  panel: Ref<HTMLElement | null>,
  active?: () => boolean,
) {
  let lastActive: HTMLElement | null = null
  const self = { close: () => (model.value = false) }

  function onKeydown(e: KeyboardEvent) {
    if (e.key !== 'Escape') return
    if (openOverlays[openOverlays.length - 1] !== self) return
    model.value = false
  }

  watch(model, (open) => {
    if (open) {
      if (active && !active()) return
      lastActive = document.activeElement as HTMLElement | null
      openOverlays.push(self)
      document.addEventListener('keydown', onKeydown)
      void nextTick(() => panel.value?.focus())
    } else {
      const i = openOverlays.indexOf(self)
      if (i >= 0) openOverlays.splice(i, 1)
      document.removeEventListener('keydown', onKeydown)
      if (lastActive && lastActive.isConnected) lastActive.focus()
      lastActive = null
    }
  })
}
