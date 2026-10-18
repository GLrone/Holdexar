/**
 * 统一 Activity 模型与类型定义
 * 灵动岛多活动系统的底层事实模型：
 * 长期任务、瞬态通知与系统状态统一收口为此结构。
 */

/** 活动状态枚举 */
export type ActivityStatus = 'pending' | 'running' | 'success' | 'failed' | 'attention'

/** 活动展示分类 */
export type ActivityKind = 'task' | 'notice' | 'system'

/** 活动视觉语义色彩 */
export type ActivityTone = 'success' | 'error' | 'warning' | 'info' | 'progress' | 'accent'

/** 核心 Activity 模型接口 */
export interface Activity {
  /** 稳定唯一标识（如 'crawler:job', 'bills:sync', 'proxy:kernel-download', 'notice:1'） */
  id: string
  /** 业务来源与所属模块（如 'crawler', 'updater', 'proxypool', 'bills', 'cdk', 'notice'） */
  source: string
  /** 展示分类 */
  kind: ActivityKind
  /** 状态枚举 */
  status: ActivityStatus
  /** 标题：单行收起态文案 */
  title: string
  /** 摘要详情：展开态详细文案 */
  detail?: string
  /** 图标名称（对应 icons.ts 认可图标） */
  icon?: string
  /** 可选业务图片标识；仅明确指向单款游戏时传入封面 */
  iconImageUrl?: string
  /** 图标语义色彩 */
  tone?: ActivityTone
  /** 进度 0-100；null 为滑动进度条；undefined 为无进度条 */
  progress?: number | null
  /** 优先级数值，越大越优先排前 */
  priority: number
  /** 是否需要用户立即关注（严重异常或阻塞交互的事项） */
  needsAttention?: boolean
  /** 创建时间戳（毫秒） */
  createdAt: number
  /** 最近更新时间戳（毫秒） */
  updatedAt: number
  /** 自动消失时长（毫秒），0 或未传表示不自动消失 */
  duration?: number
  /** 失败重试动作回调 */
  retry?: (() => void) | null
  /** 跳转目标路由 */
  to?: string
  /** 跳转按钮文案 */
  toLabel?: string
  /** 扩展元数据 */
  metadata?: Record<string, unknown>
}

/** 注册新活动或更新活动的入参选项 */
export interface ActivityRegistration {
  id: string
  source: string
  kind?: ActivityKind
  status?: ActivityStatus
  title: string
  detail?: string
  icon?: string
  iconImageUrl?: string
  tone?: ActivityTone
  progress?: number | null
  priority?: number
  needsAttention?: boolean
  duration?: number
  retry?: (() => void) | null
  to?: string
  toLabel?: string
  metadata?: Record<string, unknown>
}
