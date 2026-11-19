import { activityManager } from './activity/activityManager.ts'
import { islandController } from './activity/islandController.ts'

export type IslandTone = 'success' | 'error' | 'warning' | 'info' | 'progress'

export interface IslandNotice {
  id: number
  tone: IslandTone
  text: string
  detail: string
  sticky: boolean
  duration: number
  retry: (() => void) | null
  to: string
  toLabel: string
  iconImageUrl?: string
}

const QUEUE_MAX = 4
let seq = 0

export function pushNotice(notice: Omit<IslandNotice, 'id'>): number {
  const loading = activityManager.activities.find(a => a.source === 'notice' && a.status === 'running')
  if (loading) activityManager.remove(loading.id)
  const id = ++seq
  activityManager.register({
    id: `notice:${id}`,
    source: 'notice',
    kind: 'notice',
    status: notice.tone === 'error' ? 'failed' : (notice.sticky ? 'running' : 'success'),
    title: notice.text,
    detail: notice.detail,
    iconImageUrl: notice.iconImageUrl,
    tone: notice.tone,
    duration: notice.sticky ? 0 : notice.duration,
    retry: notice.retry,
    to: notice.to,
    toLabel: notice.toLabel,
    needsAttention: notice.tone === 'error',
  })
  const notices = activityManager.activities.filter(a => a.source === 'notice')
  for (const overflow of notices.slice(0, -QUEUE_MAX)) activityManager.remove(overflow.id)
  return id
}

export function dropNotice(id: number) {
  activityManager.remove(`notice:${id}`)
}

export function updateNotice(id: number, text: string) {
  activityManager.update(`notice:${id}`, { title: text })
}

export function setExpanded(on: boolean) {
  islandController.requestExpanded(on)
}

export function beginTask(key: string, text: string, opts?: { percent?: number | null; to?: string }) {
  activityManager.register({
    id: `task:${key}`,
    source: key.split('-')[0] || 'task',
    kind: 'task',
    status: 'running',
    title: text,
    progress: opts?.percent ?? null,
    to: opts?.to ?? '',
    needsAttention: false,
  })
}

export function updateTask(key: string, text: string, percent?: number | null) {
  activityManager.update(`task:${key}`, {
    title: text,
    ...(percent !== undefined ? { progress: percent } : {}),
  })
}

export function endTask(key: string) {
  activityManager.remove(`task:${key}`)
}
