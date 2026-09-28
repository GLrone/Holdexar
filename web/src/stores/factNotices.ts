import { defineStore } from 'pinia'

import { notificationsApi, type FactNotice } from '@/api/client'
import { message } from '@/components/ui'
import { useI18n, type MessageKey } from '@/locales'

/* ════════════════════════════════════════════════════════════════════
   factNotices — 内容链事实通知桥（灵动岛动态红线）
   后端在数据链路里判定「事实真的变了」（HB 当月包换新 / Epic 喜加一轮换）
   落 fact_notices 表；本 store 只做消费：60s 轮询增量，逐条翻译上岛。
   判定不在这里做，岛不重复算「变没变」。

   游标存 localStorage：升级后的第一轮只对齐不回放历史（不轰炸），
   之后每个新事实都进岛。语言切换后的新消息按切换后词条现译。
   ════════════════════════════════════════════════════════════════════ */

const LS_KEY = 'holdexar-fact-notices-last-id'
const POLL_MS = 60_000

/** 事实形态 → 词条 key（模块级常量只存 key，渲染期 t() 现译） */
const FACT_TEXT: Record<string, MessageKey> = {
  'hb_choice:bundle_changed': 'island.fact.hbChanged',
  'epic_free:free_rotation': 'island.fact.epicRotation',
}

/** 展开态正文词条；未登记的形态不出消息（后端新事实先补词条再上岛） */
const FACT_DETAIL: Record<string, MessageKey> = {
  'hb_choice:bundle_changed': 'island.fact.hbChangedDetail',
  'epic_free:free_rotation': 'island.fact.epicRotationDetail',
}

/** 事实参数 → 词条插值参数（用户语言：结果 + 数量，不出现实现词） */
function factParams(item: FactNotice): Record<string, string | number> {
  const data = item.data ?? {}
  const count = Number(data.count ?? 0)
  if (item.source === 'hb_choice') {
    return { label: String(data.label ?? ''), count }
  }
  const titles = (data.titles ?? []).filter(Boolean).slice(0, 3).join('、')
  return { count, titles }
}

export const useFactNoticesStore = defineStore('factNotices', () => {
  let timer: ReturnType<typeof setInterval> | null = null
  let started = false
  let lastSeen = Number(localStorage.getItem(LS_KEY) || 0)

  function saveCursor(): void {
    localStorage.setItem(LS_KEY, String(lastSeen))
  }

  /** 一条事实 → 岛上消息（详情动作落仪表盘：HB/Epic 卡片都在那里） */
  function announce(item: FactNotice): void {
    const shape = `${item.source}:${item.kind}`
    const textKey = FACT_TEXT[shape]
    if (!textKey) return
    const { t } = useI18n()
    const params = factParams(item)
    message.info(t(textKey, params), {
      duration: 8000,
      detail: FACT_DETAIL[shape] ? t(FACT_DETAIL[shape], params) : '',
      to: '/dashboard',
    })
  }

  async function poll(): Promise<void> {
    try {
      // localStorage 已有游标 = 增量轮询；没有 = 先对齐（latestId 记下不弹）
      const aligned = lastSeen > 0
      const res = await notificationsApi.facts(aligned ? lastSeen : undefined)
      if (!aligned) {
        lastSeen = res.latestId
        saveCursor()
        return
      }
      for (const item of res.items) {
        if (item.id <= lastSeen) continue
        announce(item)
        lastSeen = item.id
      }
      saveCursor()
    } catch {
      /* 后端不可达：静默，下一拍再试 */
    }
  }

  function start(): void {
    if (started) return
    started = true
    void poll()
    timer = setInterval(() => void poll(), POLL_MS)
  }

  function stop(): void {
    if (timer) {
      clearInterval(timer)
      timer = null
    }
    started = false
  }

  return { start, stop }
})
