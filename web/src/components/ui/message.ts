/* ════════════════════════════════════════════════════════════════════
   message.ts — 全局 Message（Toast）服务
   替代 ElMessage 的框架内实现；视图层唯一入口：
     import { message } from '@/components/ui'
     message.success('已保存') / message.error('连接超时')
   异步进行中反馈：message.loading('获取中…') 显示持久气泡，
   任意结果消息（success/error/warning/info）弹出时自动顶替退场。
   末位 duration 可调显示时长（缺省 2400ms）。
   ════════════════════════════════════════════════════════════════════ */

export type MessageType = 'success' | 'error' | 'warning' | 'info'

const GLYPH: Record<MessageType, string> = {
  success: '✓',
  error: '✕',
  warning: '!',
  info: 'i',
}

let box: HTMLElement | null = null
let loadingEl: HTMLElement | null = null

function ensureBox(): HTMLElement {
  if (box && document.body.contains(box)) return box
  box = document.createElement('div')
  box.className = 'hl-toast-box'
  document.body.appendChild(box)
  return box
}

function leave(el: HTMLElement) {
  el.classList.add('is-leave')
  window.setTimeout(() => el.remove(), 220)
}

function dismissLoading() {
  const el = loadingEl
  loadingEl = null
  if (el) leave(el)
}

function show(type: MessageType, text: string, duration = 2400) {
  dismissLoading()
  const el = document.createElement('div')
  el.className = `hl-toast hl-toast--${type}`
  el.setAttribute('role', 'status')
  const icon = document.createElement('i')
  icon.className = 'hl-toast__icon'
  icon.textContent = GLYPH[type]
  el.appendChild(icon)
  el.appendChild(document.createTextNode(text))
  ensureBox().appendChild(el)
  window.setTimeout(() => {
    el.classList.add('is-leave')
    window.setTimeout(() => el.remove(), 220)
  }, duration)
}

/** 进行中气泡：不自动消失，等结果消息顶替（或手动调用返回的关闭函数） */
function showLoading(text: string): () => void {
  dismissLoading()
  const el = document.createElement('div')
  el.className = 'hl-toast hl-toast--loading'
  el.setAttribute('role', 'status')
  const spin = document.createElement('i')
  spin.className = 'hl-spinner hl-spinner--xs'
  el.appendChild(spin)
  el.appendChild(document.createTextNode(text))
  ensureBox().appendChild(el)
  loadingEl = el
  return () => {
    if (loadingEl === el) dismissLoading()
  }
}

/** 进度气泡句柄：update 换文案（轮询方携带实时数字），close 主动收场 */
export interface ProgressToast {
  update: (text: string) => void
  close: () => void
}

/** 进度气泡：转圈 + 可更新文本 + 细进度条。给总量未知的长任务用
 *  （如账单翻页）——条带往复滑动表示「活着」，进度实数走文本；
 *  与 loading 同占一个槽位，结果消息弹出时同样自动顶替退场。 */
function showProgress(text: string): ProgressToast {
  dismissLoading()
  const el = document.createElement('div')
  el.className = 'hl-toast hl-toast--loading hl-toast--progress'
  el.setAttribute('role', 'status')
  const spin = document.createElement('i')
  spin.className = 'hl-spinner hl-spinner--xs'
  el.appendChild(spin)
  const label = document.createElement('span')
  label.textContent = text
  el.appendChild(label)
  const bar = document.createElement('i')
  bar.className = 'hl-toast__bar'
  el.appendChild(bar)
  ensureBox().appendChild(el)
  loadingEl = el
  return {
    update: (next: string) => {
      label.textContent = next
    },
    close: () => {
      if (loadingEl === el) dismissLoading()
    },
  }
}

export const message = {
  /** duration 缺省 2400ms；需要用户看清的长文本提示可显式加长 */
  success: (text: string, duration?: number) => show('success', text, duration),
  error: (text: string, duration?: number) => show('error', text, duration),
  warning: (text: string, duration?: number) => show('warning', text, duration),
  info: (text: string, duration?: number) => show('info', text, duration),
  loading: showLoading,
  progress: showProgress,
}
