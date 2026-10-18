/**
 * Holdexar Structured UI System — 历史卡片向后兼容适配器。
 *
 * 将既有的 PilotFacts 结构平滑映射为标准 StructuredBlock，
 * 确保旧版会话记录、审批状态与流式响应零损耗兼容回放。
 */
import type { PilotFacts } from '@/api/client'
import type { StructuredBlock } from '../types'
import { validateBlock } from '../registry'

interface RawCardCandidate {
  id?: string
  pid?: string
  appid?: number
  kind?: string
  path?: string
  target?: string
  titleKey?: string
  title?: string
  description?: string
  source?: string
  updatedAt?: string
  snapshotAt?: string
  [key: string]: unknown
}

/**
 * 为旧版卡片生成稳定实例 ID，防止使用数组下标造成状态错乱与闪烁
 */
export function generateBlockId(card: RawCardCandidate, index: number): string {
  if (card.id && typeof card.id === 'string') return card.id
  if (card.pid) return `prop_${card.pid}`
  if (card.appid) return `game_${card.appid}_${card.kind || 'card'}`
  if (card.kind === 'navigate') return `nav_${card.path || card.target || index}`
  if (card.kind === 'rows' && card.titleKey) return `rows_${card.titleKey}_${index}`
  return `block_${card.kind || 'card'}_${index}`
}

/**
 * 将任意卡片输入（既有 PilotFacts 或新 StructuredBlock）规整为标准 StructuredBlock
 */
export function normalizeCardToBlock(card: PilotFacts | StructuredBlock | unknown, index = 0): StructuredBlock {
  if (!card) {
    return {
      id: `empty_${index}`,
      componentId: 'empty',
      version: '1.0',
      status: 'empty',
      data: null,
    }
  }

  // 已经是新协议块
  if (validateBlock(card)) {
    return card
  }

  const raw = card as RawCardCandidate
  // 旧版 PilotFacts 适配为 StructuredBlock
  const kind = raw.kind || 'unknown'
  const id = generateBlockId(raw, index)

  let componentId = `legacy-${kind}`
  if (
    kind === 'metric-grid' ||
    kind === 'data-table' ||
    kind === 'time-series' ||
    kind === 'coverage-matrix' ||
    kind === 'status-board'
  ) {
    componentId = kind
  }

  return {
    id,
    componentId,
    version: '1.0',
    title: raw.title,
    description: raw.description,
    source: raw.source,
    updatedAt: raw.updatedAt || raw.snapshotAt,
    density: 'standard',
    status: 'ready',
    data: card,
    meta: {
      legacyKind: kind,
    },
  }
}

/**
 * 批量规整卡片列表
 */
export function normalizeCardsToBlocks(cards: (PilotFacts | StructuredBlock | unknown)[]): StructuredBlock[] {
  if (!Array.isArray(cards)) return []
  return cards.filter(Boolean).map((card, idx) => normalizeCardToBlock(card, idx))
}
