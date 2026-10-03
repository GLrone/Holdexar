/**
 * gifting.ts — 赠礼规则计算引擎
 *
 * 新政策（跨区价格差异不再限制赠送资格，1.15 倍率转为实付口径）：
 * - 资格：任意两个解锁区之间均可赠送（旧「超出 ×1.15 不可送」门槛废除）
 * - 实付双轨：
 *   收礼方区价 ≤ 送礼方区价 × 1.15 → 按送礼方区价付款
 *   收礼方区价 > 送礼方区价 × 1.15 → 按收礼方区价付款
 * - 锁区（无法购买）仍然不可赠送
 */

export const GIFT_THRESHOLD = 1.15

export interface RegionPriceInfo {
  code: string
  nameZh: string
  cnyFen: number
  locked: boolean
}

/** 可赠送判定：双方有价、未锁区即可（新政策不限价格倍率） */
export function canGift(
  senderPriceCnyFen: number | null | undefined,
  receiverPriceCnyFen: number | null | undefined,
): boolean {
  return (
    senderPriceCnyFen != null &&
    receiverPriceCnyFen != null &&
    senderPriceCnyFen > 0 &&
    receiverPriceCnyFen > 0
  )
}

/** 该笔赠送是否落入「按收礼方区价付款」轨道（收礼方区价超出送礼方 ×1.15） */
export function isReceiverPriced(
  senderPriceCnyFen: number | null | undefined,
  receiverPriceCnyFen: number | null | undefined,
): boolean {
  if (
    senderPriceCnyFen == null ||
    receiverPriceCnyFen == null ||
    senderPriceCnyFen <= 0 ||
    receiverPriceCnyFen <= 0
  ) {
    return false
  }
  return receiverPriceCnyFen > senderPriceCnyFen * GIFT_THRESHOLD
}

/** 送礼实付金额（分）：收礼方 ≤ 送礼方 ×1.15 按送礼方区价，超出按收礼方区价 */
export function giftPayAmountFen(
  senderPriceCnyFen: number | null | undefined,
  receiverPriceCnyFen: number | null | undefined,
): number | null {
  if (
    senderPriceCnyFen == null ||
    receiverPriceCnyFen == null ||
    senderPriceCnyFen <= 0 ||
    receiverPriceCnyFen <= 0
  ) {
    return null
  }
  return isReceiverPriced(senderPriceCnyFen, receiverPriceCnyFen)
    ? receiverPriceCnyFen
    : senderPriceCnyFen
}

export interface GiftMatch {
  code: string
  cnyFen: number
}

type PriceMatrix = Record<string, [string, number, number, unknown]>

/** 送礼筛选两模式的共同判据：收礼侧区价 ≤ 送礼侧区价 ×1.15——付款双轨里
 *  落在「按送礼方区价实付」的划算轨道（超出即 isReceiverPriced 的收礼方价轨）。
 *  与后端 _gift_exists（GIFT_TRACK_BAND）同式同源，改动必须两处同步。 */
export function onSenderTrack(
  senderPriceCnyFen: number | null | undefined,
  receiverPriceCnyFen: number | null | undefined,
): boolean {
  return (
    senderPriceCnyFen != null &&
    receiverPriceCnyFen != null &&
    senderPriceCnyFen > 0 &&
    receiverPriceCnyFen > 0 &&
    !isReceiverPriced(senderPriceCnyFen, receiverPriceCnyFen)
  )
}

/** 模式一（我送出）：固定送礼方，选目标里命中判据的集合（区价升序）。 */
export function qualifyingTargets(
  matrix: PriceMatrix,
  senderCode: string,
  targetCodes: string[],
): GiftMatch[] {
  const senderFen = matrix[senderCode.toUpperCase()]?.[1] ?? 0
  if (senderFen <= 0) return []
  return targetCodes
    .filter((c) => c.toUpperCase() !== senderCode.toUpperCase())
    .map((c) => ({ code: c, cnyFen: matrix[c.toUpperCase()]?.[1] ?? 0 }))
    .filter((m) => onSenderTrack(senderFen, m.cnyFen))
    .sort((a, b) => a.cnyFen - b.cnyFen)
}

/** 模式二（送给我）：固定收礼方，其余有价区全量遍历出的可用送礼来源（区价升序）。 */
export function qualifyingSources(
  matrix: PriceMatrix,
  receiverCode: string,
): GiftMatch[] {
  const receiverFen = matrix[receiverCode.toUpperCase()]?.[1] ?? 0
  if (receiverFen <= 0) return []
  return Object.entries(matrix)
    .map(([code, cell]) => ({ code: code.toLowerCase(), cnyFen: cell[1] }))
    .filter((m) => m.code !== receiverCode.toLowerCase())
    .filter((m) => onSenderTrack(m.cnyFen, receiverFen))
    .sort((a, b) => a.cnyFen - b.cnyFen)
}

export interface GiftingRegionRow extends RegionPriceInfo {
  /** 该笔赠送实付（分） */
  payFen: number
  /** true = 超出 ×1.15，实付按收礼方区价 */
  receiverPriced: boolean
}

export interface GiftingAnalysis {
  /** 可赠送地区（payFen = target 送该区的实付） */
  canGiveTo: GiftingRegionRow[]
  /** 不可赠送地区（锁区/无价） */
  cannotGiveTo: RegionPriceInfo[]
  /** 可接收地区（payFen = 该区送 target 时对方的实付） */
  canReceiveFrom: GiftingRegionRow[]
  targetRegion: RegionPriceInfo
}

/**
 * 计算指定地区的赠礼分析
 *
 * @param regionPrices - 所有地区的价格列表
 * @param targetRegionCode - 目标地区代码 (被点击的地区)
 */
export function computeGiftingAnalysis(
  regionPrices: RegionPriceInfo[],
  targetRegionCode: string,
): GiftingAnalysis | null {
  const target = regionPrices.find((r) => r.code === targetRegionCode)
  if (!target || target.locked || target.cnyFen <= 0) return null

  const canGiveTo: GiftingRegionRow[] = []
  const cannotGiveTo: RegionPriceInfo[] = []
  const canReceiveFrom: GiftingRegionRow[] = []

  for (const r of regionPrices) {
    if (r.code === targetRegionCode) continue

    if (r.locked || r.cnyFen <= 0) {
      cannotGiveTo.push(r)
      continue
    }

    // 发起方：target → r，实付按双轨规则
    const sendPay = giftPayAmountFen(target.cnyFen, r.cnyFen) ?? r.cnyFen
    canGiveTo.push({
      ...r,
      payFen: sendPay,
      receiverPriced: isReceiverPriced(target.cnyFen, r.cnyFen),
    })

    // 接收方：r → target，实付是对方（r）付的钱
    const recvPay = giftPayAmountFen(r.cnyFen, target.cnyFen) ?? target.cnyFen
    canReceiveFrom.push({
      ...r,
      payFen: recvPay,
      receiverPriced: isReceiverPriced(r.cnyFen, target.cnyFen),
    })
  }

  const sortByPay = (a: GiftingRegionRow, b: GiftingRegionRow) =>
    a.payFen - b.payFen || a.cnyFen - b.cnyFen
  canGiveTo.sort(sortByPay)
  canReceiveFrom.sort(sortByPay)
  cannotGiveTo.sort((a, b) => a.cnyFen - b.cnyFen)

  return { canGiveTo, cannotGiveTo, canReceiveFrom, targetRegion: target }
}
