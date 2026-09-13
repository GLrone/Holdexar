/**
 * 永降/永涨徽章的时效判定。
 *
 * 徽章语义从「该游戏原价曾永久下调/上调」的历史事实，收窄为「最近一次原价
 * 跳变还在 14 天窗口内」的资讯：变化发生后 14 天自动隐藏，不再常驻。
 * 判据是后端 refresh_pp_flags 维护的 ppChangedAt（最近一次原价跳变的快照
 * 时刻）；无值（从未变化 / 尚未重算）视同过期，不展示。
 */

/** 徽章时效窗（天）：从原价跳变发生时刻起算 */
export const PP_FLAG_WINDOW_DAYS = 14

const PP_FLAG_WINDOW_MS = PP_FLAG_WINDOW_DAYS * 86_400_000

/**
 * 徽章是否仍在时效窗内。后端datetime有「空格分隔」的存量形态，
 * 解析前统一成 ISO 的 T 分隔（WebView2/Chromium 对空格格式宽容，
 * 但显式归一不依赖运行时行为）。
 */
export function isPermChangeRecent(changedAt: string | null | undefined, now = Date.now()): boolean {
  if (!changedAt) return false
  const t = Date.parse(changedAt.includes('T') ? changedAt : changedAt.replace(' ', 'T'))
  if (!Number.isFinite(t)) return false
  return now - t <= PP_FLAG_WINDOW_MS
}
