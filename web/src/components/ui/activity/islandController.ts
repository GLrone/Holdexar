/**
 * IslandController — 灵动岛展示控制层
 * 活动数据由 ActivityManager 持有；此处只维护可见槽位和用户选择顺序。
 */
import { computed, ref } from 'vue'
import { activityManager, ActivityManager } from './activityManager.ts'
import type { Activity } from './types.ts'

export const IDLE_EXPANDED_ID = '@price-status'

export interface IslandLayout {
  mainId: string | null
  secondaryId: string | null
  expandedId: string | null
  waitingIds: string[]
}

type ExpandedOrigin = 'main' | 'secondary' | 'idle' | null
export type IslandLayoutAction = 'main' | 'secondary' | 'collapse' | 'return'

interface LayoutSnapshot extends IslandLayout {
  order: string[]
  origin: ExpandedOrigin
}

function unique(ids: Array<string | null>): string[] {
  return [...new Set(ids.filter((id): id is string => Boolean(id)))]
}

export class IslandController {
  private readonly _manager: ActivityManager
  private readonly _mainId = ref<string | null>(null)
  private readonly _secondaryId = ref<string | null>(null)
  private readonly _expandedId = ref<string | null>(null)
  private readonly _pinnedId = ref<string | null>(null)
  private readonly _userSelectedId = ref<string | null>(null)
  private _order: string[] = []
  private _expandedOrigin: ExpandedOrigin = null
  private _previousLayout: LayoutSnapshot | null = null
  private _lastUrgentId: string | null = null

  readonly isExpanded = computed(() => this._expandedId.value !== null)
  readonly isOverviewOpen = ref(false)
  readonly isHovered = ref(false)
  readonly swapRequest = ref(0)
  readonly layoutRequest = ref(0)
  private _layoutAction: IslandLayoutAction | null = null

  constructor(manager: ActivityManager = activityManager) {
    this._manager = manager
    this._manager.subscribe(() => this._syncActivities())
    this._syncActivities()
  }

  get sortedActivities(): Activity[] {
    return [...this._manager.activities].sort((a, b) => {
      if (a.needsAttention && !b.needsAttention) return -1
      if (!a.needsAttention && b.needsAttention) return 1
      if (b.priority !== a.priority) return b.priority - a.priority
      if (a.status === 'running' && b.status !== 'running') return -1
      if (a.status !== 'running' && b.status === 'running') return 1
      return a.createdAt - b.createdAt
    })
  }

  get layout(): IslandLayout {
    const visible = new Set(unique([this._mainId.value, this._secondaryId.value, this._expandedId.value]))
    const waitingIds = this._order.filter((id) => !visible.has(id) && this._manager.get(id))
    return {
      mainId: this._mainId.value,
      secondaryId: this._secondaryId.value,
      expandedId: this._expandedId.value,
      waitingIds,
    }
  }

  get primaryId(): string | null {
    return this._mainId.value
  }

  get mainId(): string | null {
    return this._mainId.value
  }

  get secondaryId(): string | null {
    return this._secondaryId.value
  }

  get expandedId(): string | null {
    return this._expandedId.value
  }

  get pinnedId(): string | null {
    return this._pinnedId.value
  }

  get primaryActivity(): Activity | null {
    return this._activity(this._mainId.value)
  }

  get secondaryActivity(): Activity | null {
    return this._activity(this._secondaryId.value)
  }

  get expandedActivity(): Activity | null {
    return this._activity(this._expandedId.value)
  }

  get count(): number {
    return this._manager.count
  }

  get hasSecondary(): boolean {
    return this.secondaryActivity !== null
  }

  get waitingIds(): string[] {
    return this.layout.waitingIds
  }

  get waitingCount(): number {
    return this.waitingIds.length
  }

  get hasExpandedActivity(): boolean {
    return this.expandedActivity !== null
  }

  get isIdleExpanded(): boolean {
    return this._expandedId.value === IDLE_EXPANDED_ID
  }

  /** 副岛直接进入展开位；候补只在副岛空出时补位。 */
  openSecondary(): void {
    const selected = this._secondaryId.value
    if (!selected || !this._manager.get(selected)) return

    this._rememberLayout()
    const currentExpanded = this._expandedId.value
    if (currentExpanded && currentExpanded !== IDLE_EXPANDED_ID) {
      this._secondaryId.value = currentExpanded
      this._expandedId.value = selected
    } else {
      this._expandedId.value = selected
      this._secondaryId.value = this._nextCandidate([this._mainId.value, selected])
    }
    this._expandedOrigin = 'secondary'
    if (this._mainId.value) this._userSelectedId.value = this._mainId.value
    this._commitVisibleOrder()
  }

  /** 主岛紧凑态进入展开位；展开态只交换主岛与展开任务。 */
  openMain(): void {
    const main = this._mainId.value
    if (this.isExpanded.value && this._expandedId.value !== IDLE_EXPANDED_ID) {
      this._rememberLayout()
      const oldMain = this._mainId.value
      const oldExpanded = this._expandedId.value
      this._mainId.value = oldExpanded
      this._expandedId.value = oldMain
      this._expandedOrigin = oldMain ? 'main' : 'secondary'
      this._userSelectedId.value = oldExpanded
      this._repairSlots()
      this._commitVisibleOrder()
      return
    }

    if (this._expandedId.value === IDLE_EXPANDED_ID) {
      this._rememberLayout()
      this._expandedId.value = null
      this._expandedOrigin = null
    } else {
      this._rememberLayout()
      const selectedMain = main ?? this._nextCandidate([])
      if (!selectedMain) {
        this._expandedId.value = IDLE_EXPANDED_ID
        this._expandedOrigin = 'idle'
        this.isOverviewOpen.value = false
        return
      }

      const nextMain = this._secondaryId.value
      this._mainId.value = nextMain
      this._expandedId.value = selectedMain
      this._secondaryId.value = this._nextCandidate([selectedMain, nextMain])
      this._expandedOrigin = 'main'
      this._userSelectedId.value = nextMain
    }

    this.isOverviewOpen.value = false
    this._repairSlots()
    this._commitVisibleOrder()
  }

  /** 兼容旧副岛请求入口；语义改为副岛直开。 */
  swap(): void {
    this.openSecondary()
  }

  requestSwap(): void {
    this.swapRequest.value += 1
  }

  requestLayoutAction(action: IslandLayoutAction): void {
    this._layoutAction = action
    this.layoutRequest.value += 1
  }

  takeLayoutAction(): IslandLayoutAction | null {
    const action = this._layoutAction
    this._layoutAction = null
    return action
  }

  requestExpanded(expanded: boolean): void {
    if (expanded && !this.isExpanded.value) this.requestLayoutAction('main')
    if (!expanded && this.isExpanded.value) this.requestLayoutAction('collapse')
  }

  /** 恢复最近一次槽位转换前的布局。 */
  returnPreviousLayout(): void {
    const previous = this._previousLayout
    if (!previous) {
      this.collapseExpanded()
      return
    }

    const active = new Set(this._manager.activities.map((activity) => activity.id))
    this._mainId.value = this._valid(previous.mainId, active)
    this._secondaryId.value = this._valid(previous.secondaryId, active)
    this._expandedId.value = previous.expandedId === IDLE_EXPANDED_ID
      ? (active.size ? null : IDLE_EXPANDED_ID)
      : this._valid(previous.expandedId, active)
    this._expandedOrigin = previous.origin
    this._order = previous.order.filter((id) => active.has(id))
    this._previousLayout = null
    this._repairSlots()
    this._commitVisibleOrder()
  }

  /** 展开任务回到进入展开位前对应的紧凑槽位。 */
  collapseExpanded(): void {
    const expanded = this._expandedId.value
    if (!expanded) return
    if (expanded === IDLE_EXPANDED_ID) {
      this._expandedId.value = null
      this._expandedOrigin = null
      this._previousLayout = null
      return
    }

    const origin = this._expandedOrigin
    if (origin === 'main') {
      const displacedMain = this._mainId.value
      this._mainId.value = expanded
      this._secondaryId.value = displacedMain === expanded ? this._nextCandidate([expanded]) : displacedMain
    } else if (origin === 'secondary') {
      const displacedSecondary = this._secondaryId.value
      this._secondaryId.value = expanded
      if (!this._mainId.value && displacedSecondary) this._mainId.value = displacedSecondary
    } else if (!this._mainId.value) {
      this._mainId.value = expanded
    } else if (!this._secondaryId.value) {
      this._secondaryId.value = expanded
    }

    this._expandedId.value = null
    this._expandedOrigin = null
    this._previousLayout = null
    this._repairSlots()
    this._commitVisibleOrder()
  }

  /** 总览选择指定活动为主岛。 */
  selectPrimary(id: string): void {
    if (!this._manager.get(id)) return
    const oldMain = this._mainId.value
    if (this._expandedId.value === id) {
      this._expandedId.value = null
      this._expandedOrigin = null
    }
    this._mainId.value = id
    this._userSelectedId.value = id
    if (!this._secondaryId.value || this._secondaryId.value === id) {
      this._secondaryId.value = oldMain && oldMain !== id && oldMain !== this._expandedId.value
        ? oldMain
        : this._nextCandidate([id, this._expandedId.value])
    }
    this._previousLayout = null
    this._repairSlots()
    this._commitVisibleOrder()
  }

  pin(id?: string): void {
    const target = id ?? this._mainId.value
    if (!target || !this._manager.get(target)) return
    this._pinnedId.value = target
    this._putInMain(target)
    this._syncActivities()
  }

  togglePin(id?: string): void {
    const target = id ?? this._mainId.value
    if (!target) return
    if (this._pinnedId.value === target) this.unpin()
    else this.pin(target)
  }

  unpin(): void {
    this._pinnedId.value = null
    if (!this._userSelectedId.value) this._order = this.sortedActivities.map((activity) => activity.id)
    this._syncActivities()
  }

  toggleOverview(): void {
    this.setOverview(!this.isOverviewOpen.value)
  }

  resetUserSelection(): void {
    this._userSelectedId.value = null
    this._order = this.sortedActivities.map((activity) => activity.id)
    this._syncActivities()
  }

  /** 保持旧展开 API；主岛任务进入独立展开槽，空闲价格态使用展示占位 ID。 */
  setExpanded(expanded: boolean): void {
    if (expanded) {
      if (!this.isExpanded.value) this.openMain()
      this.isOverviewOpen.value = false
    } else {
      this.collapseExpanded()
    }
  }

  setOverview(open: boolean): void {
    this.isOverviewOpen.value = open
    if (open && this.isExpanded.value) this.collapseExpanded()
    if (open) this._previousLayout = null
  }

  setHovered(hovered: boolean): void {
    this.isHovered.value = hovered
  }

  private _activity(id: string | null): Activity | null {
    if (!id || id === IDLE_EXPANDED_ID) return null
    return this._manager.get(id) ?? null
  }

  private _valid(id: string | null, active: Set<string>): string | null {
    return id && active.has(id) ? id : null
  }

  private _nextCandidate(excluded: Array<string | null>): string | null {
    const unavailable = new Set(unique(excluded))
    return this._order.find((id) => this._manager.get(id) && !unavailable.has(id)) ?? null
  }

  private _rememberLayout(): void {
    const current = this.layout
    this._previousLayout = { ...current, waitingIds: [...current.waitingIds], order: [...this._order], origin: this._expandedOrigin }
  }

  private _commitVisibleOrder(): void {
    const waiting = this.layout.waitingIds
    const expanded = this._expandedId.value === IDLE_EXPANDED_ID ? null : this._expandedId.value
    this._order = unique([this._mainId.value, this._secondaryId.value, expanded, ...waiting])
  }

  private _putInMain(id: string): void {
    if (this._expandedId.value === id) {
      this._expandedId.value = null
      this._expandedOrigin = null
    }
    if (this._secondaryId.value === id) this._secondaryId.value = this._mainId.value
    else if (this._mainId.value && this._mainId.value !== id && this._secondaryId.value === null) {
      this._secondaryId.value = this._mainId.value
    }
    this._mainId.value = id
    this._previousLayout = null
    this._repairSlots()
    this._commitVisibleOrder()
  }

  private _syncActivities(): void {
    const active = new Set(this._manager.activities.map((activity) => activity.id))
    const hadOrder = this._order.length > 0
    const filteredOrder = this._order.filter((id) => active.has(id))
    const additions = this.sortedActivities.filter((activity) => !filteredOrder.includes(activity.id))
    this._order = filteredOrder

    if (!hadOrder && active.size) {
      this._order = this.sortedActivities.map((activity) => activity.id)
    } else if (additions.length) {
      if (!this._pinnedId.value && !this._userSelectedId.value) {
        this._order = this.sortedActivities.map((activity) => activity.id)
      } else {
        for (const activity of additions) {
          if (activity.needsAttention) this._order.unshift(activity.id)
          else this._order.push(activity.id)
        }
      }
    }

    if (this._pinnedId.value && !active.has(this._pinnedId.value)) this._pinnedId.value = null
    if (this._userSelectedId.value && !active.has(this._userSelectedId.value)) this._userSelectedId.value = null
    if (this._expandedId.value && this._expandedId.value !== IDLE_EXPANDED_ID && !active.has(this._expandedId.value)) {
      this._expandedId.value = null
      this._expandedOrigin = null
      this._previousLayout = null
    }
    if (this._expandedId.value === IDLE_EXPANDED_ID && active.size) {
      this._expandedId.value = null
      this._expandedOrigin = null
    }

    const urgent = this.sortedActivities.find((activity) => activity.needsAttention)?.id ?? null
    if (urgent) {
      this._moveToFront(urgent)
      if (this._mainId.value !== urgent) this._putInMain(urgent)
      this._lastUrgentId = urgent
    } else {
      if (this._lastUrgentId && !this._pinnedId.value && !this._userSelectedId.value) {
        this._order = this.sortedActivities.map((activity) => activity.id)
      }
      this._lastUrgentId = null
      const preferred = this._pinnedId.value ?? this._userSelectedId.value
      if (preferred && active.has(preferred) && this._mainId.value !== preferred) this._putInMain(preferred)
    }

    this._repairSlots()
    this._commitVisibleOrder()
  }

  private _moveToFront(id: string): void {
    this._order = [id, ...this._order.filter((candidate) => candidate !== id)]
  }

  private _repairSlots(): void {
    const active = new Set(this._manager.activities.map((activity) => activity.id))
    if (this._mainId.value && !active.has(this._mainId.value)) this._mainId.value = null
    if (this._secondaryId.value && !active.has(this._secondaryId.value)) this._secondaryId.value = null
    if (this._expandedId.value && this._expandedId.value !== IDLE_EXPANDED_ID && !active.has(this._expandedId.value)) {
      this._expandedId.value = null
      this._expandedOrigin = null
    }

    const occupied = unique([this._mainId.value, this._secondaryId.value, this._expandedId.value])
    if (occupied.length !== [this._mainId.value, this._secondaryId.value, this._expandedId.value].filter(Boolean).length) {
      if (this._secondaryId.value === this._mainId.value || this._secondaryId.value === this._expandedId.value) {
        this._secondaryId.value = null
      }
      if (this._mainId.value === this._expandedId.value) this._mainId.value = null
    }

    if (!this._mainId.value && this._secondaryId.value) {
      this._mainId.value = this._secondaryId.value
      this._secondaryId.value = null
    }
    if (!this._mainId.value && this._expandedId.value !== IDLE_EXPANDED_ID) {
      const candidate = this._nextCandidate([this._expandedId.value])
      this._mainId.value = candidate
    }
    if (!this._secondaryId.value && this._mainId.value) {
      this._secondaryId.value = this._nextCandidate([this._mainId.value, this._expandedId.value])
    }
  }
}

export const islandController = new IslandController()
