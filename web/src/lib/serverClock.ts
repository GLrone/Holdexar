/** 服务端钟与本机钟的偏移（serverNow − 本机此刻）：数据年龄一律按服务端钟
 *  计，本机钟漂移不改变「数据有多旧」的结论。serverNow 缺失或不可解析按
 *  零偏移——没有可靠参照就不做校正，不引入新的猜测。 */
export function serverOffsetMs(serverNowIso: string | null | undefined, localNowMs: number): number {
  if (!serverNowIso) return 0
  const serverMs = Date.parse(serverNowIso)
  if (!Number.isFinite(serverMs)) return 0
  return serverMs - localNowMs
}
