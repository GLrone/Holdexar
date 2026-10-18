/**
 * Holdexar Structured UI System (HSUI) — 核心契约与类型定义。
 *
 * 为全系统（Agent 领航台、普通业务视图、仪表盘、报告面板）提供统一的数据驱动、
 * 可组合、可交互展示组件协议。
 */
import type { MessageKey } from '@/locales'

export type StructuredDensity = 'compact' | 'standard' | 'expanded' | 'embedded'

export type StructuredStatus =
  | 'pending'
  | 'loading'
  | 'partial'
  | 'ready'
  | 'empty'
  | 'error'
  | 'cancelled'

export type StructuredHost = 'pilot-drawer' | 'pilot-page' | 'view-page' | 'dashboard'

export interface StructuredAction {
  id: string
  label: string
  icon?: string
  tone?: 'default' | 'primary' | 'success' | 'warn' | 'bad'
  disabled?: boolean
  tooltip?: string
  handler?: (block: StructuredBlock<unknown>) => void | Promise<void>
  /** 是否需要二次确认 */
  confirm?: {
    title: string
    content: string
  }
}

/** 统一结构化展示块协议 */
export interface StructuredBlock<T = unknown> {
  /** 稳定实例 ID，用于 DOM 复用、流式增量定位与会话回放 */
  id: string
  /** 注册组件唯一标识（如 "metric-grid", "data-table", "time-series" 等） */
  componentId: string
  /** 协议或组件契约版本号 */
  version: string
  /** 展示标题（可选） */
  title?: string
  /** 副标题或简短描述 */
  description?: string
  /** 数据来源（如 "Steam Store", "本地代理池", "调度器"） */
  source?: string
  /** 数据时刻（ISO 8601 或时间戳字符串） */
  updatedAt?: string
  /** 展示密度（默认为 standard） */
  density?: StructuredDensity
  /** 状态模型 */
  status: StructuredStatus
  /** 错误或告警摘要 */
  errorMessage?: string
  /** 核心强类型业务数据载荷 */
  data: T
  /** 受控操作项（复制、展开、刷新、详情等） */
  actions?: StructuredAction[]
  /** 内部元数据（如原始工具调用 trace_id 等） */
  meta?: Record<string, unknown>
}

/** 组件注册元数据 */
export interface ComponentMeta {
  componentId: string
  nameKey: MessageKey
  descriptionKey: MessageKey
  /** 供 Agent 意图识别与组件选择理解的英文/无中文能力描述 */
  agentDescription: string
  /** 支持的展示密度 */
  supportedDensities: StructuredDensity[]
  /** 支持的宿主环境 */
  supportedHosts: StructuredHost[]
  /** 默认标题键 */
  defaultTitleKey?: MessageKey
  /** 适用的业务数据 Schema 或类型名称 */
  schemaType?: string
}

/* ─────────────────────────────────────────────────────────────
   P0 核心数据载荷类型定义
   ───────────────────────────────────────────────────────────── */

/** P0-A: MetricGrid 指标项定义 */
export interface MetricItem {
  id: string
  label: string
  value: string | number
  unit?: string
  status?: 'normal' | 'good' | 'warn' | 'bad'
  /** 相对变化（如 "+12%", "-¥30"） */
  diff?: string
  diffDirection?: 'up' | 'down' | 'flat'
  /** 变化趋势的业务好坏（如涨价是 bad，覆盖率提升是 good） */
  diffTone?: 'good' | 'warn' | 'bad' | 'neutral'
  /** 基准值/对比值（如 "史低 ¥28"） */
  benchmark?: string
  tooltip?: string
  subText?: string
  clickable?: boolean
}

export interface MetricGridData {
  items: MetricItem[]
  columns?: number
}

/** P0-B: DataTable 表格列与数据定义 */
export interface TableColumnDef {
  prop: string
  label: string
  width?: string
  minWidth?: string
  align?: 'left' | 'center' | 'right'
  numeric?: boolean
  sortable?: boolean
  highlight?: boolean
  badgeTone?: (val: unknown, row: Record<string, unknown>) => 'good' | 'warn' | 'bad' | 'info' | undefined
  formatter?: (val: unknown, row: Record<string, unknown>) => string
}

export interface TableRowAction {
  id: string
  label: string
  icon?: string
  tone?: 'default' | 'primary' | 'warn' | 'bad'
  handler: (row: Record<string, unknown>) => void
}

export interface DataTableData {
  columns: TableColumnDef[]
  rows: Record<string, unknown>[]
  total?: number
  pageSize?: number
  currentPage?: number
  rowKey?: string
  rowActions?: TableRowAction[]
  emptyText?: string
}

/** P0-C: TimeSeries 时间序列点与线定义 */
export interface TimeSeriesPoint {
  /** 时间戳（毫秒）或日期字符串 */
  t: number | string
  /** 数值 */
  v: number
  /** 可选附带标签或日期描述 */
  label?: string
  /** 额外数据项 */
  extra?: Record<string, unknown>
}

export interface TimeSeriesLine {
  id: string
  name: string
  /** 曲线类型：阶梯线（价格必选）、折线、平滑曲线 */
  curveType: 'step' | 'line' | 'smooth'
  tone?: 'accent' | 'success' | 'warn' | 'danger'
  data: TimeSeriesPoint[]
  unit?: string
}

export interface TimeSeriesMarker {
  type: 'min' | 'max' | 'horizontal-line' | 'point'
  value?: number
  timestamp?: number | string
  label: string
  tone?: 'accent' | 'success' | 'warn' | 'danger'
}

export interface TimeSeriesData {
  lines: TimeSeriesLine[]
  markers?: TimeSeriesMarker[]
  /** 初始时间窗（天数，0 表示全部） */
  windowDays?: number
  unit?: string
  showArea?: boolean
  yAxisMin?: number
  yAxisMax?: number
}

/** P0-D: CoverageMatrix 覆盖与状态矩阵定义 */
export interface MatrixCell {
  colId: string
  status: 'ok' | 'failed' | 'not_attempted' | 'expired' | 'locked' | 'unknown'
  label?: string
  tooltip?: string
  reason?: string
  lastSuccessAt?: string
  lastAttemptAt?: string
  data?: Record<string, unknown>
}

export interface MatrixRow {
  rowId: string
  name: string
  subText?: string
  avatar?: string
  cells: Record<string, MatrixCell>
}

export interface CoverageMatrixData {
  columns: { id: string; label: string; flag?: string }[]
  rows: MatrixRow[]
  stats?: {
    totalUnits: number
    okUnits: number
    failedUnits: number
    coverageRate: number
  }
}

/** P0-E: StatusBoard 状态看板定义 */
export interface StatusBoardItem {
  id: string
  name: string
  status: 'online' | 'offline' | 'degraded' | 'busy' | 'idle' | 'warn'
  statusText?: string
  subTitle?: string
  metrics?: { label: string; value: string | number }[]
  updatedAt?: string
  actionLabel?: string
  onAction?: () => void
}

export interface StatusBoardGroup {
  id: string
  title: string
  status?: 'healthy' | 'warning' | 'critical'
  items: StatusBoardItem[]
}

export interface StatusBoardData {
  groups: StatusBoardGroup[]
  overallStatus?: 'healthy' | 'warning' | 'critical'
  summaryText?: string
}
