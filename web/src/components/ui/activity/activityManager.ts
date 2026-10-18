/**
 * ActivityManager — 统一活动协调器
 * 管理全局业务活动与提示的生命周期、去重、状态跃迁与生命周期定时器。
 * 纯状态逻辑，不直接耦合具体的动画与展示组件。
 */
import { reactive, shallowRef } from 'vue'
import type { Activity, ActivityRegistration, ActivityTone } from './types.ts'

export class ActivityManager {
  private readonly _items = shallowRef<Activity[]>([])
  private readonly _timers = new Map<string, ReturnType<typeof setTimeout>>()
  private readonly _mutedTones = reactive<Record<string, boolean>>({})
  private readonly _listeners = new Set<() => void>()

  /** 优先级默认值配置 */
  private static readonly DEFAULT_PRIORITIES = {
    notice: 20,
    task: 30,
    system: 10,
    attention: 50,
  }

  /** 获取当前所有活动快照 */
  get activities(): readonly Activity[] {
    return this._items.value
  }

  /** 获取在跑活动数量 */
  get count(): number {
    return this._items.value.length
  }

  /** 注册或原地更新活动；相同 id 自动去重并保留既有创建时间戳 */
  register(reg: ActivityRegistration): Activity | null {
    if (reg.tone && this._mutedTones[reg.tone]) {
      return null
    }

    const now = Date.now()
    const index = this._items.value.findIndex((item) => item.id === reg.id)
    const kind = reg.kind ?? 'task'
    const status = reg.status ?? (kind === 'task' ? 'running' : 'pending')
    const needsAttention = reg.needsAttention ?? (status === 'failed')

    const fallbackPriority = needsAttention
      ? ActivityManager.DEFAULT_PRIORITIES.attention
      : ActivityManager.DEFAULT_PRIORITIES[kind] ?? 20

    if (index >= 0) {
      const existing = this._items.value[index]
      const updated: Activity = {
        ...existing,
        source: reg.source,
        kind,
        status,
        title: reg.title,
        detail: reg.detail ?? existing.detail,
        icon: reg.icon ?? existing.icon,
        tone: reg.tone ?? existing.tone,
        progress: reg.progress !== undefined ? reg.progress : existing.progress,
        priority: reg.priority ?? existing.priority,
        needsAttention,
        updatedAt: now,
        duration: reg.duration !== undefined ? reg.duration : existing.duration,
        retry: reg.retry !== undefined ? reg.retry : existing.retry,
        to: reg.to !== undefined ? reg.to : existing.to,
        toLabel: reg.toLabel !== undefined ? reg.toLabel : existing.toLabel,
        metadata: reg.metadata ? { ...existing.metadata, ...reg.metadata } : existing.metadata,
      }

      const next = [...this._items.value]
      next[index] = updated
      this._items.value = next
      this._armDuration(updated)
      this._notify()
      return updated
    }

    const created: Activity = {
      id: reg.id,
      source: reg.source,
      kind,
      status,
      title: reg.title,
      detail: reg.detail,
      icon: reg.icon,
      tone: reg.tone,
      progress: reg.progress !== undefined ? reg.progress : undefined,
      priority: reg.priority ?? fallbackPriority,
      needsAttention,
      createdAt: now,
      updatedAt: now,
      duration: reg.duration,
      retry: reg.retry ?? null,
      to: reg.to ?? '',
      toLabel: reg.toLabel ?? '',
      metadata: reg.metadata ?? {},
    }

    this._items.value = [...this._items.value, created]
    this._armDuration(created)
    this._notify()
    return created
  }

  /** 原位更新指定活动的部分属性；进度变化时保证不重建整个活动 */
  update(id: string, patch: Partial<ActivityRegistration>): Activity | null {
    const index = this._items.value.findIndex((item) => item.id === id)
    if (index < 0) return null

    const existing = this._items.value[index]
    const nextStatus = patch.status ?? existing.status
    const progress = patch.progress !== undefined
      ? patch.progress
      : (nextStatus === 'success' || nextStatus === 'failed' ? undefined : existing.progress)

    const updated: Activity = {
      ...existing,
      ...(patch.source ? { source: patch.source } : {}),
      ...(patch.kind ? { kind: patch.kind } : {}),
      status: nextStatus,
      ...(patch.title !== undefined ? { title: patch.title } : {}),
      ...(patch.detail !== undefined ? { detail: patch.detail } : {}),
      ...(patch.icon !== undefined ? { icon: patch.icon } : {}),
      ...(patch.tone !== undefined ? { tone: patch.tone } : {}),
      progress,
      ...(patch.priority !== undefined ? { priority: patch.priority } : {}),
      ...(patch.needsAttention !== undefined ? { needsAttention: patch.needsAttention } : {}),
      ...(patch.duration !== undefined ? { duration: patch.duration } : {}),
      ...(patch.retry !== undefined ? { retry: patch.retry } : {}),
      ...(patch.to !== undefined ? { to: patch.to } : {}),
      ...(patch.toLabel !== undefined ? { toLabel: patch.toLabel } : {}),
      ...(patch.metadata ? { metadata: { ...existing.metadata, ...patch.metadata } } : {}),
      updatedAt: Date.now(),
    }

    const next = [...this._items.value]
    next[index] = updated
    this._items.value = next

    if (patch.duration !== undefined) {
      this._armDuration(updated)
    }
    this._notify()
    return updated
  }

  /** 将活动置为完成状态并附带自动移除延时 */
  complete(id: string, opts?: { title?: string; detail?: string; duration?: number; tone?: ActivityTone }): void {
    this.update(id, {
      status: 'success',
      tone: opts?.tone ?? 'success',
      needsAttention: false,
      progress: undefined,
      ...(opts?.title ? { title: opts.title } : {}),
      ...(opts?.detail ? { detail: opts.detail } : {}),
      duration: opts?.duration ?? 3000,
    })
  }

  /** 将活动置为失败状态，标记需要用户关注 */
  fail(id: string, opts?: { title?: string; detail?: string; retry?: () => void }): void {
    this.update(id, {
      status: 'failed',
      tone: 'error',
      needsAttention: true,
      progress: undefined,
      ...(opts?.title ? { title: opts.title } : {}),
      ...(opts?.detail ? { detail: opts.detail } : {}),
      ...(opts?.retry ? { retry: opts.retry } : {}),
      duration: 0,
    })
  }

  /** 移除指定活动并撤销其定时器 */
  remove(id: string): boolean {
    this._clearTimer(id)
    const prevLen = this._items.value.length
    this._items.value = this._items.value.filter((item) => item.id !== id)
    const removed = this._items.value.length < prevLen
    if (removed) {
      this._notify()
    }
    return removed
  }

  /** 查询指定活动 */
  get(id: string): Activity | undefined {
    return this._items.value.find((item) => item.id === id)
  }

  /** 按语义色彩静音，同时清除已有匹配项并阻止后续入队 */
  muteTone(tone: ActivityTone): void {
    this._mutedTones[tone] = true
    const toRemove = this._items.value.filter((item) => item.tone === tone)
    for (const item of toRemove) {
      this.remove(item.id)
    }
  }

  /** 解除语义静音 */
  unmuteTone(tone: ActivityTone): void {
    this._mutedTones[tone] = false
  }

  /** 查询语义静音状态 */
  isToneMuted(tone: ActivityTone): boolean {
    return Boolean(this._mutedTones[tone])
  }

  /** 订阅活动变更 */
  subscribe(fn: () => void): () => void {
    this._listeners.add(fn)
    return () => this._listeners.delete(fn)
  }

  /** 清空全部活动与计时器 */
  clear(): void {
    for (const id of this._timers.keys()) {
      this._clearTimer(id)
    }
    this._items.value = []
    this._notify()
  }

  private _armDuration(activity: Activity): void {
    this._clearTimer(activity.id)
    if (!activity.duration || activity.duration <= 0) return

    const timer = setTimeout(() => {
      this.remove(activity.id)
    }, activity.duration)

    this._timers.set(activity.id, timer)
  }

  private _clearTimer(id: string): void {
    const timer = this._timers.get(id)
    if (timer !== undefined) {
      clearTimeout(timer)
      this._timers.delete(id)
    }
  }

  private _notify(): void {
    for (const listener of this._listeners) {
      try {
        listener()
      } catch {
        /* 避免监听器报错打断状态更新 */
      }
    }
  }
}

/** 全局活动管理器单例 */
export const activityManager = new ActivityManager()
