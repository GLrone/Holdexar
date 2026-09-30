/**
 * 钱包同步成功的显示判定（顶栏胶囊 / 弹层 / 设置页共用）。
 *
 * 后端快照字段：checked_at = 最近一次尝试时刻（成功失败都写），
 * ok_at = 上次成功获取余额的时刻（失败改写时保留）。后端轮转同步节奏
 * 不受本判定影响；这里只决定**显示层**何时把同步视为成功——
 * 上次成功获取余额在缓存窗内即视为成功（余额本身是窗内的真实数据），
 * 窗外失败才如实呈现失败态。
 */
import type { WalletSnapshot } from '@/api/client'

/** 同步成功缓存窗：上次成功获取余额在此窗内，显示层视为同步成功 */
export const WALLET_SYNC_OK_WINDOW_MS = 10 * 60 * 1000

/** 后端时刻是本地时区裸 ISO（无时区后缀）；截到毫秒避开微秒尾巴的解析差异 */
function parseLocalMs(iso: string | null | undefined): number | null {
  if (!iso) return null
  const t = Date.parse(iso.length > 23 ? iso.slice(0, 23) : iso)
  return Number.isFinite(t) ? t : null
}

/** 同步是否视为成功：最近一次尝试成功，或上次成功获取余额在缓存窗内 */
export function walletSyncOk(
  wallet: WalletSnapshot | null | undefined,
  now = Date.now(),
): boolean {
  if (!wallet) return false
  if (wallet.check_ok) return true
  const okAt = parseLocalMs(wallet.ok_at)
  return okAt !== null && now - okAt < WALLET_SYNC_OK_WINDOW_MS
}

/** 展示用同步时刻：缓存窗内取上次成功时刻，窗外取最近尝试时刻 */
export function walletSyncedAt(wallet: WalletSnapshot): string | null {
  if (wallet.check_ok) return wallet.checked_at
  const okAt = parseLocalMs(wallet.ok_at)
  if (okAt !== null && Date.now() - okAt < WALLET_SYNC_OK_WINDOW_MS) {
    return wallet.ok_at ?? null
  }
  return wallet.checked_at
}
