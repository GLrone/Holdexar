/* 许可证 / 账单行 × 家庭库命中 + 价格上下文的共享层（账单页两个页签共用）。

   命中三级（先精确后模糊）：
   ① appid 精确——steam_fetch 从许可行名称列的商店链接提取（sub 行/外部导出
      为 null，跳过此级）；
   ② 归一化名字精确——小写 + 去 ™®© + 只留字母数字与 CJK；
   ③ 版本后缀剥离后再比对——Deluxe/Gold/Complete Edition、Demo、Playtest、
      Dedicated Server 这类一侧带一侧不带的变体（英文整词剥，CJK 只剥固定
      尾缀词，避免误伤含这些词的游戏名本体）。

   价格上下文 = /games/{appid}/price-context 的批量端点，按（appid, 日期）
   模块级缓存，跨页签、跨页签重挂载复用。 */
import { ref } from 'vue'

import { gamesApi, type FamilyLibGame, type GamePriceContext, type GamePriceContextItem } from '@/api/client'

export interface HitGame {
  appid: number
  name: string
  headerImage: string | null
}

export interface LibIndex {
  byAppid: Map<number, HitGame>
  byName: Map<string, HitGame>
  byAlt: Map<string, HitGame>
}

/** 基础归一：小写 + 去 ™®© + 只留字母数字与 CJK（忽略空格、标点、符号） */
export function normKey(name: string): string {
  return name
    .toLowerCase()
    .replace(/[™®©]/g, '')
    .replace(/[^a-z0-9\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+/g, '')
}

// 版本/形态后缀（英文按词边界整词剥；数字不打头——「Portal 2」这类续作
// 数字尾缀不会被卷进去）
const EN_SUFFIX_RE =
  /\b(dedicated\s+server|server|playtest|standard|deluxe|gold|ultimate|premium|complete|collector'?s?|enhanced|definitive|expanded|remastered|goty|game\s+of\s+the\s+year|demo|edition|version)\b/gi
// CJK 只剥固定尾缀词（fused 无词边界，逐词剥会误伤本体）
const CJK_SUFFIX_RE = /(标准版|豪华版|黄金版|终极版|完全版|年度版|加强版|收藏版|白金版|测试版|体验版)$/

/** 版本后缀剥离后的次级归一键（一侧带 Edition/Demo 一侧不带时仍能对上） */
export function altKey(name: string): string {
  return normKey((name || '').replace(EN_SUFFIX_RE, ' ').replace(CJK_SUFFIX_RE, ''))
}

export function buildIndex(games: FamilyLibGame[]): LibIndex {
  const byAppid = new Map<number, HitGame>()
  const byName = new Map<string, HitGame>()
  const byAlt = new Map<string, HitGame>()
  for (const g of games) {
    const entry: HitGame = { appid: g.appid, name: g.name, headerImage: g.headerImage }
    if (!byAppid.has(g.appid)) byAppid.set(g.appid, entry)
    const k = normKey(g.name ?? '')
    if (k && !byName.has(k)) byName.set(k, entry)
    const a = altKey(g.name ?? '')
    if (a && !byAlt.has(a)) byAlt.set(a, entry)
  }
  return { byAppid, byName, byAlt }
}

/** 命中解析：appid 精确 → 名字精确 → 版本剥离后精确（双向）；未命中 null */
export function hitOfIndex(
  index: LibIndex,
  name: string,
  appid?: number | null,
): HitGame | null {
  if (appid != null && appid > 0) {
    const direct = index.byAppid.get(appid)
    if (direct) return direct
  }
  const k = normKey(name)
  if (k) {
    const hit = index.byName.get(k) ?? index.byAlt.get(k)
    if (hit) return hit
  }
  const a = altKey(name)
  if (a && a !== k) {
    const hit = index.byName.get(a) ?? index.byAlt.get(a)
    if (hit) return hit
  }
  return null
}

/* ── 价格上下文：模块级共享缓存 + 批量补拉 ─────────────────────── */

const _ctxMap = ref<Record<string, GamePriceContextItem>>({})
const _pending = new Set<string>()
const _BATCH_MAX = 200

export function ctxKey(appid: number, date: string): string {
  return `${appid}:${date}`
}

export function ctxOf(appid: number, date: string): GamePriceContextItem | undefined {
  return _ctxMap.value[ctxKey(appid, date)]
}

/** 批量确保上下文已拉取（重复对去重；失败对落 null 形态，会话内不再重试） */
export async function ensureCtx(pairs: Array<{ appid: number; date: string }>): Promise<void> {
  const batch = pairs
    .filter((p) => p.appid > 0 && p.date)
    .filter((p) => {
      const key = ctxKey(p.appid, p.date)
      return !_ctxMap.value[key] && !_pending.has(key)
    })
    .slice(0, _BATCH_MAX)
  if (!batch.length) return
  for (const p of batch) _pending.add(ctxKey(p.appid, p.date))
  try {
    const res = await gamesApi.priceContextBatch(batch)
    const next = { ..._ctxMap.value }
    for (const r of res.results) next[ctxKey(r.appid, r.date)] = r
    _ctxMap.value = next
  } catch {
    /* 拉取失败：价格段静默缺席——落 null 形态避免每个触发点都重打一遍 */
    const next = { ..._ctxMap.value }
    for (const p of batch) next[ctxKey(p.appid, p.date)] = { appid: p.appid, date: p.date, at: null, lowest: null }
    _ctxMap.value = next
  } finally {
    for (const p of batch) _pending.delete(ctxKey(p.appid, p.date))
  }
}

export interface PriceParts {
  atFen: number
  lowFen: number
  isAtLowest: boolean
}

/** 价格文案的数值部分（文案组装留给视图，t() 在渲染期取）；无当时价 → null */
export function priceParts(ctx: GamePriceContextItem | undefined | null): PriceParts | null {
  if (!ctx?.at) return null
  const atFen = ctx.at.cnyFen
  const lowFen = ctx.lowest ? Math.min(ctx.lowest.cnyFen, atFen) : atFen
  return { atFen, lowFen, isAtLowest: lowFen >= atFen }
}
