/** SteamID64 ↔ 好友码换算（全站唯一出口）。

 * 好友码 = SteamID64 - 76561197960265728（Steam 好友码列表可复制的整数）。
 * BigInt 换算避免 17 位数字超 Number 安全整数；非法输入返回空串。
 */
const FRIEND_CODE_OFFSET = 76561197960265728n

export function friendCodeOf(steamId: string): string {
  const sid = (steamId || '').trim()
  if (!/^\d{17}$/.test(sid)) return ''
  return String(BigInt(sid) - FRIEND_CODE_OFFSET)
}
