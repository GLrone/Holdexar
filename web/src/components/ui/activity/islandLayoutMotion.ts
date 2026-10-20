import { createRectKeyframes, islandSwapDuration, islandSwapEasing, type IslandMotionRect } from './islandMotionGeometry.ts'

interface IslandTextOffset {
  x: number
  y: number
}

interface IslandVisual {
  id: string
  slot: string
  shell: HTMLElement
  logo: HTMLElement | null
  content: HTMLElement | null
  shellRect: IslandMotionRect
  logoRect: IslandMotionRect | null
  textOffset: IslandTextOffset
}

function rectOf(element: HTMLElement): IslandMotionRect {
  const rect = element.getBoundingClientRect()
  return {
    left: rect.left,
    top: rect.top,
    width: rect.width,
    height: rect.height,
    borderRadius: getComputedStyle(element).borderRadius,
  }
}

function textOffsetOf(shell: HTMLElement, shellRect: IslandMotionRect): IslandTextOffset {
  const anchor = shell.querySelector<HTMLElement>('.hl-island__text')
    ?? shell.querySelector<HTMLElement>('[data-island-content]')
  if (!anchor) return { x: 0, y: 0 }
  const rect = anchor.getBoundingClientRect()
  return {
    x: rect.left - shellRect.left,
    y: rect.top - shellRect.top,
  }
}

function createOverlay(element: HTMLElement, rect: IslandMotionRect, kind: 'shell' | 'logo' | 'content'): HTMLElement {
  const rawClone = element.cloneNode(true) as HTMLElement
  const clone = kind === 'content' && element.parentElement
    ? (element.parentElement.cloneNode(false) as HTMLElement)
    : rawClone
  if (clone !== rawClone) clone.appendChild(rawClone)

  const slotHost = element.closest<HTMLElement>('[data-island-slot]')
  if (slotHost) {
    for (const attr of slotHost.getAttributeNames()) {
      if (attr.startsWith('data-v-')) clone.setAttribute(attr, '')
    }
  }

  if (kind === 'content') {
    clone.querySelectorAll<HTMLElement>('[data-activity-logo]').forEach((logo) => {
      logo.style.visibility = 'hidden'
    })
    const contentNodes = [clone, ...clone.querySelectorAll<HTMLElement>('*')]
    contentNodes.forEach((node) => {
      node.style.setProperty('animation', 'none')
      node.style.setProperty('transition', 'none')
    })
  }

  clone.removeAttribute('id')
  clone.removeAttribute('data-v-app')
  clone.removeAttribute('data-activity-id')
  clone.removeAttribute('data-activity-logo')
  clone.removeAttribute('data-island-slot')
  clone.removeAttribute('data-queue-origin')
  clone.setAttribute('aria-hidden', 'true')
  clone.inert = true
  clone.querySelectorAll('[id]').forEach((child) => child.removeAttribute('id'))
  clone.querySelectorAll('[data-activity-id], [data-activity-logo], [data-island-slot]').forEach((child) => {
    child.removeAttribute('data-activity-id')
    child.removeAttribute('data-activity-logo')
    child.removeAttribute('data-island-slot')
  })

  const style = getComputedStyle(element)
  Object.assign(clone.style, {
    position: 'fixed',
    left: `${rect.left}px`,
    top: `${rect.top}px`,
    right: 'auto',
    bottom: 'auto',
    width: `${rect.width}px`,
    height: `${rect.height}px`,
    borderRadius: rect.borderRadius,
    margin: '0',
    flex: 'none',
    transform: 'none',
    transition: 'none',
    pointerEvents: 'none',
    zIndex: kind === 'shell' ? '10001' : kind === 'content' ? '10002' : '10003',
    opacity: '1',
  })

  if (kind === 'shell') {
    clone.className = ''
    Object.assign(clone.style, {
      background: style.backgroundColor,
      boxShadow: style.boxShadow,
      border: 'none',
      outline: 'none',
      overflow: 'hidden',
    })
    clone.replaceChildren()
  }

  if (kind === 'logo') {
    Object.assign(clone.style, {
      display: style.display,
      alignItems: style.alignItems,
      justifyContent: style.justifyContent,
      backgroundColor: style.backgroundColor,
      color: style.color,
    })
  }

  if (kind === 'content') {
    Object.assign(clone.style, {
      background: 'transparent',
      boxShadow: 'none',
      border: 'none',
      outline: 'none',
    })
  }

  document.body.appendChild(clone)
  return clone
}

function sameRect(first: IslandMotionRect, second: IslandMotionRect): boolean {
  return Math.abs(first.left - second.left) < 1
    && Math.abs(first.top - second.top) < 1
    && Math.abs(first.width - second.width) < 1
    && Math.abs(first.height - second.height) < 1
}

function collectVisuals(host: HTMLElement): Map<string, IslandVisual> {
  const visuals = new Map<string, IslandVisual>()
  host.querySelectorAll<HTMLElement>('[data-activity-id][data-island-slot]').forEach((shell) => {
    const id = shell.dataset.activityId
    if (!id) return
    const shellRect = rectOf(shell)
    const logo = shell.querySelector<HTMLElement>('[data-activity-logo]')
    const content = shell.querySelector<HTMLElement>('[data-island-content]')
    const textOffset = textOffsetOf(shell, shellRect)
    visuals.set(id, {
      id,
      slot: shell.dataset.islandSlot ?? '',
      shell: createOverlay(shell, shellRect, 'shell'),
      logo: logo ? createOverlay(logo, rectOf(logo), 'logo') : null,
      content: content ? createOverlay(content, shellRect, 'content') : null,
      shellRect,
      logoRect: logo ? rectOf(logo) : null,
      textOffset,
    })
  })
  return visuals
}

function readTargets(host: HTMLElement): Map<string, { shell: HTMLElement; logo: HTMLElement | null; content: HTMLElement | null; slot: string }> {
  const targets = new Map<string, { shell: HTMLElement; logo: HTMLElement | null; content: HTMLElement | null; slot: string }>()
  host.querySelectorAll<HTMLElement>('[data-activity-id][data-island-slot]').forEach((shell) => {
    const id = shell.dataset.activityId
    if (!id) return
    targets.set(id, {
      shell,
      logo: shell.querySelector<HTMLElement>('[data-activity-logo]'),
      content: shell.querySelector<HTMLElement>('[data-island-content]'),
      slot: shell.dataset.islandSlot ?? '',
    })
  })
  return targets
}

export class IslandLayoutMotion {
  private readonly host: HTMLElement
  private readonly source: Map<string, IslandVisual>
  private readonly queueOrigin: DOMRect | null
  private readonly overlays = new Set<HTMLElement>()
  private readonly animations: Animation[] = []
  private readonly hidden: Array<{ element: HTMLElement; property: string; value: string; priority: string }> = []
  private disposed = false

  constructor(host: HTMLElement) {
    this.host = host
    const queueBadge = host.querySelector<HTMLElement>('[data-queue-origin]')
    this.queueOrigin = queueBadge?.getBoundingClientRect() ?? null
    this.source = collectVisuals(host)
    this.source.forEach((visual) => {
      this.overlays.add(visual.shell)
      if (visual.logo) this.overlays.add(visual.logo)
      if (visual.content) this.overlays.add(visual.content)
    })
  }

  async play(): Promise<void> {
    if (this.disposed) return
    if (matchMedia('(prefers-reduced-motion: reduce)').matches) {
      this.finish()
      return
    }

    const targets = readTargets(this.host)
    for (const [id, visual] of this.source) {
      if (!targets.has(id)) {
        this.removeVisual(visual)
      }
    }

    const queuePoint = this.queueOrigin
    const transitions: Promise<unknown>[] = []

    for (const [id, target] of targets) {
      const source = this.source.get(id)
      const targetShellRect = rectOf(target.shell)
      const targetLogoRect = target.logo ? rectOf(target.logo) : null
      const isNewCandidate = !source && id !== '@price-status' && this.queueOrigin !== null
      const sourceRect = source?.shellRect ?? (isNewCandidate && queuePoint
        ? {
            left: queuePoint.left + queuePoint.width / 2 - targetShellRect.width / 2,
            top: queuePoint.top + queuePoint.height / 2 - targetShellRect.height / 2,
            width: targetShellRect.width,
            height: targetShellRect.height,
            borderRadius: '50%',
          }
        : null)
      const moved = source
        ? source.slot !== target.slot || !sameRect(source.shellRect, targetShellRect)
        : isNewCandidate

      if (!moved || !sourceRect) {
        if (source) this.removeVisual(source)
        continue
      }

      this.setTemporaryStyle(target.shell, 'transition', 'none')
      const shellClone = source?.shell ?? createOverlay(target.shell, sourceRect, 'shell')
      this.overlays.add(shellClone)
      this.setTemporaryStyle(target.shell, 'background-color', 'transparent')
      this.setTemporaryStyle(target.shell, 'box-shadow', 'none')
      if (target.logo) this.setTemporaryStyle(target.logo, 'opacity', '0')
      const shellMotion = shellClone.animate(createRectKeyframes(sourceRect, targetShellRect), {
        duration: islandSwapDuration,
        easing: islandSwapEasing,
        fill: 'both',
      })
      this.animations.push(shellMotion)
      transitions.push(shellMotion.finished.catch(() => undefined))

      const targetTextOffset = textOffsetOf(target.shell, targetShellRect)
      const deltaX = source?.content && target.content ? targetTextOffset.x - source.textOffset.x : 0
      const deltaY = source?.content && target.content ? targetTextOffset.y - source.textOffset.y : 0

      if (source?.content) {
        Object.assign(source.content.style, {
          position: 'absolute',
          left: '0px',
          top: '0px',
          width: `${sourceRect.width}px`,
          height: `${sourceRect.height}px`,
        })
        shellClone.appendChild(source.content)
        const oldContentFade = source.content.animate(
          [
            { opacity: 1, transform: 'translate(0px, 0px)' },
            { opacity: 0, transform: `translate(${deltaX}px, ${deltaY}px)` },
          ],
          {
            duration: islandSwapDuration,
            easing: islandSwapEasing,
            fill: 'both',
          },
        )
        this.animations.push(oldContentFade)
        transitions.push(oldContentFade.finished.catch(() => undefined))
      }

      if (target.content) {
        this.setTemporaryStyle(target.content, 'opacity', '0')
        this.setTemporaryStyle(target.content, 'transition', 'none')

        const contentClone = createOverlay(target.content, targetShellRect, 'content')
        this.overlays.add(contentClone)
        Object.assign(contentClone.style, {
          position: 'absolute',
          left: '0px',
          top: '0px',
          width: `${targetShellRect.width}px`,
          height: `${targetShellRect.height}px`,
        })
        shellClone.appendChild(contentClone)
        const contentReveal = contentClone.animate(
          [
            { opacity: 0, transform: `translate(${-deltaX}px, ${-deltaY}px)` },
            { opacity: 1, transform: 'translate(0px, 0px)' },
          ],
          {
            duration: islandSwapDuration,
            easing: islandSwapEasing,
            fill: 'both',
          },
        )
        this.animations.push(contentReveal)
        transitions.push(contentReveal.finished.catch(() => undefined))
      }

      if (target.logo && targetLogoRect) {
        let logoClone = source?.logo ?? null
        let logoStart = source?.logoRect ?? null
        if (!logoClone && isNewCandidate && queuePoint) {
          logoClone = createOverlay(target.logo, targetLogoRect, 'logo')
          this.overlays.add(logoClone)
          const startSize = Math.min(18, targetLogoRect.width, targetLogoRect.height)
          logoStart = {
            left: queuePoint.left + queuePoint.width / 2 - startSize / 2,
            top: queuePoint.top + queuePoint.height / 2 - startSize / 2,
            width: startSize,
            height: startSize,
            borderRadius: '50%',
          }
        }
        if (logoClone && logoStart) {
          const logoMotion = logoClone.animate(createRectKeyframes(logoStart, targetLogoRect), {
            duration: islandSwapDuration,
            easing: islandSwapEasing,
            fill: 'both',
          })
          this.animations.push(logoMotion)
          transitions.push(logoMotion.finished.catch(() => undefined))
        }
      }
    }

    if (!transitions.length) {
      this.finish()
      return
    }
    await Promise.all(transitions)
    this.finish()
  }

  finish(): void {
    if (this.disposed) return
    this.disposed = true
    for (const animation of this.animations) {
      try {
        if (animation.playState !== 'finished') animation.finish()
      } catch {
        animation.cancel()
      }
    }
    for (const animation of this.animations) animation.cancel()
    for (const overlay of this.overlays) overlay.remove()
    this.overlays.clear()
    for (const { element, property, value, priority } of this.hidden) {
      if (value) element.style.setProperty(property, value, priority)
      else element.style.removeProperty(property)
    }
    this.hidden.length = 0
  }

  private setTemporaryStyle(element: HTMLElement, property: string, value: string): void {
    this.hidden.push({
      element,
      property,
      value: element.style.getPropertyValue(property),
      priority: element.style.getPropertyPriority(property),
    })
    element.style.setProperty(property, value)
  }

  private removeVisual(visual: IslandVisual): void {
    for (const overlay of [visual.shell, visual.logo, visual.content]) {
      if (!overlay) continue
      overlay.remove()
      this.overlays.delete(overlay)
    }
  }
}
