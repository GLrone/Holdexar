/**
 * IslandController — 灵动岛展示控制层
 * 负责主岛、副岛、多活动排序、用户手动选择与总览面板状态控制。
 * 业务数据变化不会无故冲刷用户的当前选择。
 */
import { ref } from 'vue'
import { activityManager, ActivityManager } from './activityManager.ts'
import type { Activity } from './types.ts'

export class IslandController {
  private readonly _manager: ActivityManager
  private readonly _primaryId = ref<string | null>(null)
  private readonly _secondaryId = ref<string | null>(null)
  private readonly _pinnedId = ref<string | null>(null)
  private readonly _userSelectedId = ref<string | null>(null)
  private _order: string[] = []

  readonly isExpanded = ref(false)
  readonly isOverviewOpen = ref(false)
  readonly isHovered = ref(false)
  readonly swapRequest = ref(0)

  constructor(manager: ActivityManager = activityManager) {
    this._manager = manager
    this._manager.subscribe(() => {
      this._syncOrder()
    })
  }

  /** 获取按业务重要度与时间排序的活动队列 */
  get sortedActivities(): Activity[] {
    const list = [...this._manager.activities]
    return list.sort((a, b) => {
      // 1. 需要紧急关注的事项置顶
      if (a.needsAttention && !b.needsAttention) return -1
      if (!a.needsAttention && b.needsAttention) return 1

      // 2. 优先级降序
      if (b.priority !== a.priority) {
        return b.priority - a.priority
      }

      // 3. 正在运行的任务优先于已完成或等待项
      if (a.status === 'running' && b.status !== 'running') return -1
      if (a.status !== 'running' && b.status === 'running') return 1

      // 4. 同优先级与同状态下较早创建的任务排在前面，保持展示稳定性（避免被普通进度更新冲刷）
      return a.createdAt - b.createdAt
    })
  }

  /** 当前主岛活动 ID */
  get primaryId(): string | null {
    return this._primaryId.value
  }

  /** 当前副岛活动 ID */
  get secondaryId(): string | null {
    return this._secondaryId.value
  }

  /** 用户固定活动 ID */
  get pinnedId(): string | null {
    return this._pinnedId.value
  }

  /** 当前主岛展示的 Activity 对象 */
  get primaryActivity(): Activity | null {
    if (!this._primaryId.value) return null
    return this._manager.get(this._primaryId.value) ?? null
  }

  /** 当前副岛展示的 Activity 对象 */
  get secondaryActivity(): Activity | null {
    if (!this._secondaryId.value) return null
    return this._manager.get(this._secondaryId.value) ?? null
  }

  /** 活动总数 */
  get count(): number {
    return this._manager.count
  }

  /** 是否有副岛可用（至少 2 项） */
  get hasSecondary(): boolean {
    return this._manager.count >= 2
  }

  /** 额外等待活动数量（扣除主副岛后） */
  get waitingCount(): number {
    return Math.max(0, this._manager.count - 2)
  }

  /** 点击副岛：交换主岛与副岛；若队列存在额外活动，原主岛进入队列尾部参与轮转 */
  swap(): void {
    if (this._order.length <= 1) return

    if (this._order.length === 2) {
      const [first, second] = this._order
      this._order = [second, first]
      this._userSelectedId.value = second
      this._primaryId.value = second
      this._secondaryId.value = first
      return
    }

    // 3 项及以上：队首移至队尾，原副岛顺升为主岛，下一项成为副岛
    const head = this._order.shift()!
    this._order.push(head)
    const newPrimary = this._order[0]
    this._userSelectedId.value = newPrimary
    this._primaryId.value = newPrimary
    this._secondaryId.value = this._order[1] ?? null
  }

  requestSwap(): void {
    this.swapRequest.value += 1
  }

  /** 手动选择指定活动作为主岛（如从总览列表中点击某项） */
  selectPrimary(id: string): void {
    if (!this._manager.get(id)) return
    this._userSelectedId.value = id
    const idx = this._order.indexOf(id)
    if (idx > 0) {
      this._order.splice(idx, 1)
      this._order.unshift(id)
    }
    this._primaryId.value = this._order[0] ?? null
    this._secondaryId.value = this._order[1] ?? null
  }

  /** 固定当前主岛活动（防止后台新任务替换） */
  pin(id?: string): void {
    const target = id ?? this._primaryId.value
    if (target && this._manager.get(target)) {
      this._pinnedId.value = target
      this._syncOrder()
    }
  }

  /** 切换固定状态 */
  togglePin(id?: string): void {
    const target = id ?? this._primaryId.value
    if (!target) return
    if (this._pinnedId.value === target) {
      this.unpin()
    } else {
      this.pin(target)
    }
  }

  /** 解除固定 */
  unpin(): void {
    this._pinnedId.value = null
    if (!this._userSelectedId.value) {
      this._order = this.sortedActivities.map((a) => a.id)
    }
    this._syncOrder()
  }

  /** 切换总览面板开合 */
  toggleOverview(): void {
    this.setOverview(!this.isOverviewOpen.value)
  }

  /** 重置用户手动选择（恢复按队列优先级自动展示） */
  resetUserSelection(): void {
    this._userSelectedId.value = null
    this._order = this.sortedActivities.map((a) => a.id)
    this._syncOrder()
  }

  /** 设置展开态 */
  setExpanded(expanded: boolean): void {
    this.isExpanded.value = expanded
    if (expanded) {
      this.isOverviewOpen.value = false
    }
  }

  /** 设置总览面板开合 */
  setOverview(open: boolean): void {
    this.isOverviewOpen.value = open
    if (open) {
      this.isExpanded.value = false
    }
  }

  /** 设置指针悬停态 */
  setHovered(hovered: boolean): void {
    this.isHovered.value = hovered
  }

  /** 同步活动队列显示顺序 */
  private _syncOrder(): void {
    const activeIds = new Set(this._manager.activities.map((a) => a.id))

    // 1. 过滤已不存在的活动
    this._order = this._order.filter((id) => activeIds.has(id))

    // 2. 清理失效的固定与选中状态
    if (this._pinnedId.value && !activeIds.has(this._pinnedId.value)) {
      this._pinnedId.value = null
    }
    if (this._userSelectedId.value && !activeIds.has(this._userSelectedId.value)) {
      this._userSelectedId.value = null
    }

    // 3. 若无用户主动固定与手动选择，以 sortedActivities 作为基准顺序
    if (!this._pinnedId.value && !this._userSelectedId.value) {
      this._order = this.sortedActivities.map((a) => a.id)
    } else {
      const sorted = this.sortedActivities
      for (const act of sorted) {
        if (!this._order.includes(act.id)) {
          if (act.needsAttention) {
            this._order.unshift(act.id)
          } else {
            this._order.push(act.id)
          }
        }
      }
    }

    // 4. 检查是否有存量活动突发 needsAttention，必须优先置顶
    const urgentId = this._order.find((id) => this._manager.get(id)?.needsAttention)
    if (urgentId && this._order[0] !== urgentId) {
      const idx = this._order.indexOf(urgentId)
      if (idx > 0) {
        this._order.splice(idx, 1)
        this._order.unshift(urgentId)
      }
    }

    // 5. 若有固定项且无紧急事件，固定项保持主岛
    if (this._pinnedId.value && !urgentId) {
      const pinIdx = this._order.indexOf(this._pinnedId.value)
      if (pinIdx > 0) {
        this._order.splice(pinIdx, 1)
        this._order.unshift(this._pinnedId.value)
      }
    }

    // 6. 若有用户手动选定项且无紧急事件与固定项，手动选定项保持主岛
    if (this._userSelectedId.value && !urgentId && !this._pinnedId.value) {
      const selIdx = this._order.indexOf(this._userSelectedId.value)
      if (selIdx > 0) {
        this._order.splice(selIdx, 1)
        this._order.unshift(this._userSelectedId.value)
      }
    }

    // 7. 同步对外响应式引用
    this._primaryId.value = this._order[0] ?? null
    this._secondaryId.value = this._order[1] ?? null
  }
}

/** 全局灵动岛控制器单例 */
export const islandController = new IslandController()
