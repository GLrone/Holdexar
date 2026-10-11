/**
 * 页面元素选择器（仿 ZCode useWebElementPicker）
 *
 * 交互流程：点击按钮 → 进入选择模式（crosshair cursor + hover 高亮 overlay）→
 * 点击元素 → 采集摘要 → 退出选择模式 → 以 chip 形式显示在输入框。
 * Esc 取消选择。
 *
 * 与 ZCode 的关键差异：不采集完整 HTML/CSS/rect，只采集「agent 需要知道
 * 用户指代什么」的最小信息——标签、文本、selector、role、accessibleName。
 * 同一页面的其他组件仍可选择（不做全局屏蔽），agent 默认无法读取的组件
 * 不生效由后端按页面路由白名单决定，前端不做拦截。
 */
import { ref, onBeforeUnmount } from 'vue'

export interface PickedElement {
  id: string
  /** 元素标签名（小写） */
  tag: string
  /** 元素可访问名称（aria-label / alt / title / 内联文本） */
  label: string
  /** 元素文本内容（截断 200 字符） */
  text: string
  /** CSS selector 路径（从元素到 body 的唯一路径） */
  selector: string
  /** ARIA role（显式或隐式） */
  role: string
  /** 所在页面路由路径 */
  route: string
  /** 采集时间戳 */
  capturedAt: number
}

const MAX_TEXT_CHARS = 200
const MAX_LABEL_CHARS = 120

function truncate(value: string | null | undefined, max: number): string {
  const s = (value ?? '').replace(/\s+/g, ' ').trim()
  return s.length > max ? s.slice(0, max) + '...' : s
}

function cssEscape(value: string): string {
  if (typeof CSS !== 'undefined' && CSS.escape) return CSS.escape(value)
  return value.replace(/[^a-zA-Z0-9_-]/g, '\\$&')
}

function getImplicitRole(el: Element): string {
  const tag = el.tagName.toLowerCase()
  if (tag === 'button') return 'button'
  if (tag === 'a' && el.hasAttribute('href')) return 'link'
  if (tag === 'img') return 'img'
  if (tag === 'input') {
    const type = (el.getAttribute('type') ?? 'text').toLowerCase()
    if (type === 'checkbox') return 'checkbox'
    if (type === 'radio') return 'radio'
    if (type === 'button' || type === 'submit' || type === 'reset') return 'button'
    return 'textbox'
  }
  if (tag === 'textarea') return 'textbox'
  if (tag === 'select') return 'combobox'
  if (tag === 'nav') return 'navigation'
  if (tag === 'main') return 'main'
  if (/^h[1-6]$/.test(tag)) return 'heading'
  return ''
}

function getAccessibleName(el: Element): string {
  const labelledBy = el.getAttribute('aria-labelledby')
  if (labelledBy) {
    const label = labelledBy
      .split(/\s+/)
      .map((id) => document.getElementById(id)?.textContent ?? '')
      .join(' ')
    const v = truncate(label, MAX_LABEL_CHARS)
    if (v) return v
  }
  return truncate(
    el.getAttribute('aria-label') ||
      el.getAttribute('alt') ||
      el.getAttribute('title') ||
      el.getAttribute('placeholder') ||
      (el as HTMLElement).innerText ||
      el.textContent,
    MAX_LABEL_CHARS,
  )
}

function getElementText(el: Element): string {
  if (el instanceof HTMLInputElement) {
    if (el.type.toLowerCase() === 'password') return '[password]'
    return truncate(
      el.getAttribute('aria-label') ||
        el.getAttribute('placeholder') ||
        el.name ||
        el.type,
      MAX_TEXT_CHARS,
    )
  }
  if (el instanceof HTMLTextAreaElement) {
    return truncate(
      el.getAttribute('aria-label') ||
        el.getAttribute('placeholder') ||
        el.name ||
        'textarea',
      MAX_TEXT_CHARS,
    )
  }
  return truncate((el as HTMLElement).innerText || el.textContent, MAX_TEXT_CHARS)
}

function getSelector(el: Element): string {
  if (el.id) return `#${cssEscape(el.id)}`
  const parts: string[] = []
  let current: Element | null = el
  while (current && current.nodeType === Node.ELEMENT_NODE && parts.length < 8) {
    const tag = current.tagName.toLowerCase()
    if (current.id) {
      parts.unshift(`${tag}#${cssEscape(current.id)}`)
      break
    }
    const classNames = Array.from(current.classList)
      .filter(Boolean)
      .slice(0, 2)
      .map((c) => `.${cssEscape(c)}`)
      .join('')
    let part = `${tag}${classNames}`
    const parent = current.parentElement
    if (parent) {
      const sameTag = Array.from(parent.children).filter(
        (s): s is Element => s instanceof Element && s.tagName === current!.tagName,
      )
      if (sameTag.length > 1) part += `:nth-of-type(${sameTag.indexOf(current) + 1})`
    }
    parts.unshift(part)
    current = parent
  }
  return parts.join(' > ')
}

/** 从元素向上找到最近的带 data-pilot-section 标记的容器 */
function getSection(el: Element): string {
  const node = el.closest('[data-pilot-section]')
  return node?.getAttribute('data-pilot-section') || ''
}

export function useElementPicker() {
  const isPicking = ref(false)
  const picked = ref<PickedElement[]>([])
  let overlay: HTMLDivElement | null = null
  let label: HTMLDivElement | null = null
  let hoveredEl: Element | null = null
  let settled = false

  function cleanup() {
    document.removeEventListener('mousemove', onMouseMove, true)
    document.removeEventListener('click', onClick, true)
    document.removeEventListener('keydown', onKeyDown, true)
    overlay?.remove()
    label?.remove()
    overlay = null
    label = null
    document.documentElement.style.cursor = ''
    isPicking.value = false
  }

  function updateOverlay(target: Element | null) {
    if (!target || !overlay || !label) {
      if (overlay) overlay.style.display = 'none'
      if (label) label.style.display = 'none'
      return
    }
    // 不选到自己（picker UI）
    if (target === overlay || target === label || label.contains(target)) {
      overlay.style.display = 'none'
      label.style.display = 'none'
      return
    }
    const rect = target.getBoundingClientRect()
    if (rect.width <= 0 || rect.height <= 0) {
      overlay.style.display = 'none'
      label.style.display = 'none'
      return
    }
    overlay.style.display = 'block'
    overlay.style.left = `${Math.max(0, rect.left)}px`
    overlay.style.top = `${Math.max(0, rect.top)}px`
    overlay.style.width = `${rect.width}px`
    overlay.style.height = `${rect.height}px`

    // 标签框
    label.style.display = 'block'
    label.replaceChildren()
    const tag = document.createElement('div')
    tag.textContent = target.tagName.toLowerCase()
    Object.assign(tag.style, {
      color: 'var(--text-on-fill, #fff)',
      fontSize: '14px',
      fontWeight: '700',
    })
    const name = getAccessibleName(target)
    if (name) {
      const n = document.createElement('div')
      n.textContent = truncate(name, 60)
      Object.assign(n.style, {
        color: 'var(--text-on-fill, rgba(255,255,255,0.7))',
        fontSize: '12px',
        marginTop: '2px',
      })
      tag.append(n)
    }
    label.append(tag)

    const lw = label.offsetWidth || 200
    const lh = label.offsetHeight || 50
    const gap = 8
    let left = rect.left + rect.width / 2 - lw / 2
    left = Math.max(gap, Math.min(left, window.innerWidth - lw - gap))
    let top = rect.bottom + gap
    if (top + lh > window.innerHeight - gap) top = rect.top - lh - gap
    if (top < gap) top = gap
    label.style.left = `${left}px`
    label.style.top = `${top}px`
  }

  function onMouseMove(e: MouseEvent) {
    const t = e.target
    if (!(t instanceof Element)) return
    hoveredEl = t
    updateOverlay(t)
  }

  function onClick(e: MouseEvent) {
    e.preventDefault()
    e.stopPropagation()
    e.stopImmediatePropagation()
    if (!hoveredEl) {
      finish(null)
      return
    }
    finish(collectElement(hoveredEl))
  }

  function onKeyDown(e: KeyboardEvent) {
    if (e.key !== 'Escape') return
    e.preventDefault()
    e.stopPropagation()
    finish(null)
  }

  function collectElement(el: Element): PickedElement {
    return {
      id: `el-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      tag: el.tagName.toLowerCase(),
      label: getAccessibleName(el),
      text: getElementText(el),
      selector: getSelector(el),
      role: el.getAttribute('role') || getImplicitRole(el),
      route: location.hash.replace('#', '') || '/',
      capturedAt: Date.now(),
    }
  }

  function finish(element: PickedElement | null) {
    if (settled) return
    settled = true
    cleanup()
    if (element) {
      picked.value = [...picked.value, element]
    }
  }

  function startPicking() {
    if (isPicking.value) return
    isPicking.value = true
    settled = false

    overlay = document.createElement('div')
    overlay.setAttribute('data-hl-element-picker', 'overlay')
    Object.assign(overlay.style, {
      background: 'var(--accent-a20, rgba(37, 99, 235, 0.12))',
      border: '2px solid var(--accent-a, #2563eb)',
      borderRadius: '4px',
      boxShadow: '0 0 0 9999px var(--bg-overlay, rgba(15, 23, 42, 0.08))',
      boxSizing: 'border-box',
      display: 'none',
      left: '0',
      pointerEvents: 'none',
      position: 'fixed',
      top: '0',
      zIndex: '2147483647',
    } as Partial<CSSStyleDeclaration>)

    label = document.createElement('div')
    Object.assign(label.style, {
      backdropFilter: 'blur(8px)',
      background: 'var(--surface-pop, rgba(17, 24, 39, 0.92))',
      border: '1px solid var(--border-soft, rgba(255,255,255,0.14))',
      borderRadius: '8px',
      boxShadow: '0 8px 24px var(--bg-overlay, rgba(15,23,42,0.28))',
      boxSizing: 'border-box',
      color: '#f9fafb',
      display: 'none',
      font: '12px/1.4 -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif',
      left: '0',
      maxWidth: 'calc(100vw - 16px)',
      padding: '6px 12px',
      pointerEvents: 'none',
      position: 'fixed',
      top: '0',
      zIndex: '2147483647',
    } as Partial<CSSStyleDeclaration>)

    document.documentElement.append(overlay, label)
    document.documentElement.style.cursor = 'crosshair'
    document.addEventListener('mousemove', onMouseMove, true)
    document.addEventListener('click', onClick, true)
    document.addEventListener('keydown', onKeyDown, true)
  }

  function cancelPicking() {
    if (!isPicking.value) return
    settled = true
    cleanup()
  }

  function removePicked(id: string) {
    picked.value = picked.value.filter((p) => p.id !== id)
  }

  function clearPicked() {
    picked.value = []
  }

  onBeforeUnmount(() => {
    cleanup()
  })

  return {
    isPicking,
    picked,
    startPicking,
    cancelPicking,
    removePicked,
    clearPicked,
  }
}
