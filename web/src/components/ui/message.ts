/* ════════════════════════════════════════════════════════════════════
   message.ts — 全局 Message 服务
   视图层唯一入口：
     import { message } from '@/components/ui'
     message.success('已保存') / message.error('连接超时')
   本模块只负责入队，气泡渲染由 HlIsland.vue 承担（顶部悬浮胶囊）。
   进行中反馈：message.loading('获取中…') 占住展示槽位，
   任意结果消息（success/error/warning/info）入队时就地顶替。
   末位可传显示时长（缺省 2400ms），或传选项对象附带详情 / 重试 / 跳转。
   ════════════════════════════════════════════════════════════════════ */

import { dropNotice, pushNotice, updateNotice } from './island'

export type MessageType = 'success' | 'error' | 'warning' | 'info'

/** 结果消息的可选补充：展开面正文、原地重试、详情跳转 */
export interface MessageOptions {
  /** 显示时长（ms） */
  duration?: number
  /** 展开态正文，一句话说清发生了什么 */
  detail?: string
  /** 展开态「重试」动作的回调；不给则该按钮不出现 */
  retry?: () => void
  /** 展开态详情动作的跳转目标路由；不给则按语义给默认落点 */
  to?: string
  /** 详情动作按钮文案 */
  toLabel?: string
}

const DEFAULT_DURATION = 2400

function normalize(opts?: number | MessageOptions): MessageOptions {
  return typeof opts === 'number' ? { duration: opts } : (opts ?? {})
}

function show(type: MessageType, text: string, opts?: number | MessageOptions): number {
  const o = normalize(opts)
  return pushNotice({
    tone: type,
    text,
    detail: o.detail ?? '',
    sticky: false,
    duration: o.duration ?? DEFAULT_DURATION,
    retry: o.retry ?? null,
    to: o.to ?? '',
    toLabel: o.toLabel ?? '',
  })
}

/** 进行中气泡：不自动收起，等结果消息顶替（或手动调用返回的关闭函数） */
function showLoading(text: string, detail = ''): () => void {
  const id = pushNotice({
    tone: 'progress',
    text,
    detail,
    sticky: true,
    duration: 0,
    retry: null,
    to: '',
    toLabel: '',
  })
  return () => dropNotice(id)
}

/** 进度气泡句柄：update 换文案（轮询方携带实时数字），close 主动收场 */
export interface ProgressToast {
  update: (text: string) => void
  close: () => void
}

/** 进度气泡：可更新文本的进行中项，占住展示槽位直至主动收场 */
function showProgress(text: string, detail = ''): ProgressToast {
  const id = pushNotice({
    tone: 'progress',
    text,
    detail,
    sticky: true,
    duration: 0,
    retry: null,
    to: '',
    toLabel: '',
  })
  return {
    update: (next: string) => updateNotice(id, next),
    close: () => dropNotice(id),
  }
}

export const message = {
  /** 末位传 number 为显示时长；传对象可附带详情 / 重试 / 跳转 */
  success: (text: string, opts?: number | MessageOptions) => show('success', text, opts),
  error: (text: string, opts?: number | MessageOptions) => show('error', text, opts),
  warning: (text: string, opts?: number | MessageOptions) => show('warning', text, opts),
  info: (text: string, opts?: number | MessageOptions) => show('info', text, opts),
  loading: showLoading,
  progress: showProgress,
}
