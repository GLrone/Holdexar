import type { MessageKey } from '../../../locales'
import type { Activity, ActivityTone } from './types'

/** 动效类型：进度弧环绕、微脉冲呼吸、状态小圆点、静止 */
export type PresentationMotion = 'none' | 'orbit' | 'pulse' | 'dot'

/** 标识呈现模型：确保模块专属标识永远真实可见，不被通用 spinner 掩盖 */
export interface ActivityPresentation {
  /** 标识类型：标准图标或文字徽章 */
  badgeType: 'icon' | 'badge'
  /** 图标名称（对应 icons.ts） */
  iconName: string
  /** 徽章国际化词条键名（优先走 i18n 词典） */
  badgeKey?: MessageKey
  /** 徽章文字兜底（例如非词典的自定义标识） */
  badgeText?: string
  /** 色彩基调 */
  tone: ActivityTone
  /** 动态微动效 */
  motion: PresentationMotion
  /** 是否携带警示小三角标记 */
  hasWarningMarker?: boolean
  /** 是否呈现进度百分比数值 */
  showPercent?: boolean
  /** 是否呈现进度条 */
  showBar?: boolean
}

export interface ResolveOptions {
  showingPrice?: boolean
  priceRunning?: boolean
}

/**
 * 根据 Activity 实体及运行状态解析出专属视觉呈现参数。
 * 核心原则：
 * 1. 任何运行态均保留模块标识，通过独立命名的进度弧环绕表达进行中，严禁以 spinner 覆盖图标；
 * 2. 版本更新使用专属「新」徽章或 download/package 模块符号；
 * 3. 价格采集固定使用 refresh 标识；
 * 4. 代理池异常固定使用 globe 标识 + 警示标注。
 */
export function resolvePresentation(
  activity: Activity | null,
  options: ResolveOptions = {},
): ActivityPresentation {
  if (!activity) {
    if (options.showingPrice) {
      return {
        badgeType: 'icon',
        iconName: 'refresh',
        tone: 'progress',
        motion: options.priceRunning ? 'orbit' : 'none',
      }
    }
    return {
      badgeType: 'icon',
      iconName: 'info',
      tone: 'info',
      motion: 'none',
    }
  }

  const isRunning = activity.status === 'running'
  const isFailed = activity.status === 'failed'
  const isAttention = activity.status === 'attention' || Boolean(activity.needsAttention)
  const isSuccess = activity.status === 'success'

  /* 显式徽章标识重载（例如 'badge:new'） */
  if (activity.icon?.startsWith('badge:')) {
    const text = activity.icon.slice(6)
    return {
      badgeType: 'badge',
      badgeKey: text === 'new' ? 'island.badge.new' : undefined,
      badgeText: text === 'new' ? undefined : text,
      iconName: 'download',
      tone: activity.tone ?? (isAttention ? 'accent' : 'info'),
      motion: isAttention ? 'pulse' : 'none',
      hasWarningMarker: isFailed,
    }
  }

  /* 按业务模块特征收敛 */
  const src = activity.source?.toLowerCase() ?? ''

  if (src === 'updater') {
    if (activity.id === 'updater:ready' || activity.kind === 'system' || activity.metadata?.type === 'ready') {
      return {
        badgeType: 'icon',
        iconName: 'package',
        tone: 'success',
        motion: 'dot',
      }
    }
    if (activity.id === 'updater:available' || activity.metadata?.type === 'available' || (activity.status === 'attention' && activity.progress === undefined)) {
      return {
        badgeType: 'badge',
        badgeKey: 'island.badge.new',
        iconName: 'download',
        tone: 'accent',
        motion: 'pulse',
      }
    }
    if (isRunning) {
      return {
        badgeType: 'icon',
        iconName: 'download',
        tone: 'progress',
        motion: 'orbit',
        showPercent: activity.progress !== undefined && activity.progress !== null,
        showBar: true,
      }
    }
    if (isFailed) {
      return {
        badgeType: 'icon',
        iconName: 'download',
        tone: 'error',
        hasWarningMarker: true,
        motion: 'none',
      }
    }
    return {
      badgeType: 'icon',
      iconName: 'download',
      tone: activity.tone ?? 'info',
      motion: 'none',
    }
  }

  if (src === 'crawler') {
    return {
      badgeType: 'icon',
      iconName: 'refresh',
      tone: isFailed ? 'error' : isRunning ? 'progress' : isSuccess ? 'success' : (activity.tone ?? 'info'),
      motion: isRunning ? 'orbit' : 'none',
      showPercent: isRunning && activity.progress !== undefined && activity.progress !== null,
      showBar: isRunning,
      hasWarningMarker: isFailed,
    }
  }

  if (src === 'proxypool' || src === 'proxy') {
    return {
      badgeType: 'icon',
      iconName: 'globe',
      tone: isFailed || isAttention ? 'error' : isRunning ? 'progress' : (activity.tone ?? 'info'),
      motion: isRunning ? 'orbit' : isFailed || isAttention ? 'pulse' : 'none',
      hasWarningMarker: isFailed || isAttention,
      showPercent: isRunning && activity.progress !== undefined && activity.progress !== null,
      showBar: isRunning,
    }
  }

  if (src === 'notice' || src === 'price-alert') {
    return {
      badgeType: 'icon',
      iconName: 'bell',
      tone: activity.tone ?? (isAttention ? 'warning' : 'info'),
      motion: isAttention ? 'pulse' : 'none',
      hasWarningMarker: isAttention,
    }
  }

  if (src === 'backup') {
    return {
      badgeType: 'icon',
      iconName: 'database',
      tone: isRunning ? 'progress' : isFailed ? 'error' : (activity.tone ?? 'info'),
      motion: isRunning ? 'orbit' : 'none',
      hasWarningMarker: isFailed,
      showPercent: isRunning && activity.progress !== undefined && activity.progress !== null,
      showBar: isRunning,
    }
  }

  if (src === 'bills' || src === 'sync' || src === 'batch') {
    return {
      badgeType: 'icon',
      iconName: activity.icon || (src === 'bills' ? 'database' : 'refresh'),
      tone: isRunning ? 'progress' : isFailed ? 'error' : (activity.tone ?? 'info'),
      motion: isRunning ? 'orbit' : 'none',
      hasWarningMarker: isFailed,
      showPercent: isRunning && activity.progress !== undefined && activity.progress !== null,
      showBar: isRunning,
    }
  }

  /* 通用兜底 */
  const fallbackIcon =
    activity.icon || (isFailed ? 'x-circle' : isSuccess ? 'check-circle' : isRunning ? 'refresh' : 'info')
  const fallbackTone =
    activity.tone || (isFailed ? 'error' : isSuccess ? 'success' : isRunning ? 'progress' : 'info')

  return {
    badgeType: 'icon',
    iconName: fallbackIcon,
    tone: fallbackTone,
    motion: isRunning ? 'orbit' : isAttention ? 'pulse' : isSuccess ? 'dot' : 'none',
    hasWarningMarker: isFailed,
    showPercent: isRunning && activity.progress !== undefined && activity.progress !== null,
    showBar: isRunning,
  }
}
