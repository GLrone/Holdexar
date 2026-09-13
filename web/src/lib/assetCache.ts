/**
 * 图片素材复用登记处（**模块级**，跨组件实例存活）。
 *
 * **解决什么。** 封面图加载失败时 `HlGameCard` 与 `bundles` 都会重试 4 次，
 * 但重试计数和「已放弃」标记此前都写在组件内的 `ref` 里——组件一销毁就归零。
 * 于是列表里一张**永久失效**（下架包 / CDN 删档 / 旧 sub 的图被回收）的封面，
 * 每换一次筛选、每切一次板块、每重挂载一次，就要重跑一遍「裸 URL + 4 次重试」
 * = 5 次注定失败的往返。一屏 50 张卡里只要有几张死图，来回切板块就是几十次
 * 无谓请求砸到 Steam CDN 上——这就是「重复的数据不要多次请求」在图片侧的落点。
 *
 * **为什么不写预加载器、也不做 in-flight 合并。** 同一 URL 的并发图片请求浏览器
 * 自己会合并，响应也由 HTTP 缓存兜住；这两件事重造一遍只会多一层与浏览器缓存
 * 的竞态。本模块只做浏览器**做不了**的两件事：
 * ① 把「这张已经坏了」这个判断从组件生命周期里提出来，跨实例共享；
 * ② 记住**哪一次尝试成功过**，下次直接用那个 URL——原先的重试 URL 拼了
 *    `Date.now()`，使每次重试都是一个全新 URL，既绕开浏览器缓存、也绕开 CDN
 *    边缘缓存，命中率恒为 0；换成稳定后缀后，同一张图的重试在任何组件实例、
 *    任何挂载轮次里都是同一个 URL，缓存才能真正生效。
 *
 * **为什么死图状态带 TTL 而不是永久。** CDN 抖动 / 代理闪断造成的失败不该把一张
 * 正常封面在黑名单里钉死一整个会话；但窗口要长到足以让「来回切板块」这类密集
 * 重复全部命中。10 分钟是两端折中：窗口内零请求，窗口过后自动放行一次探活，
 * 素材恢复即自愈（成功路径会把它从死名单里摘掉）。
 */

/** 失败登记的有效期：窗口内的重复挂载不再重试 */
export const ASSET_DEAD_TTL_MS = 10 * 60_000

/** 单张素材自觉重试上限（两个调用点同义，统一收到这里，避免各自漂移） */
export const ASSET_RETRY_MAX = 4

/** 登记表容量上限。长会话里无界增长就是泄漏；超限按插入序淘汰最早的一条 */
const REGISTRY_MAX = 512

interface AssetRecord {
  status: 'ok' | 'dead'
  /** status='ok' 时**确实加载成功过的那个 URL**（裸 URL 或带 hlretry 后缀） */
  url: string
  at: number
}

const registry = new Map<string, AssetRecord>()

/** 取出仍在有效期内的登记；过期即摘除（返回 undefined 表示「当作没见过」） */
function liveRecord(base: string): AssetRecord | undefined {
  const rec = registry.get(base)
  if (!rec) return undefined
  if (rec.status === 'dead' && Date.now() - rec.at > ASSET_DEAD_TTL_MS) {
    registry.delete(base)
    return undefined
  }
  return rec
}

function remember(base: string, rec: AssetRecord): void {
  if (registry.size >= REGISTRY_MAX) {
    const oldest = registry.keys().next().value
    if (oldest !== undefined) registry.delete(oldest)
  }
  registry.set(base, rec)
}

/**
 * 这张素材是否处于「已知失效」窗口内。
 *
 * `true` 时调用方应**直接落占位、一次网络都不发**——这正是本模块存在的理由，
 * 也是组件内 `coverBroken` 的初值来源。
 */
export function isAssetDead(url: string | null | undefined): boolean {
  if (!url) return true
  return liveRecord(url)?.status === 'dead'
}

/**
 * 该用哪个 URL 发请求。
 *
 * 成功过的素材直接返回**当初成功的那一个** URL：裸 URL 若反过来是坏的（代理
 * 缓存了坏响应、或该图只在带后缀时可达），这一步就省掉每轮挂载的一次白跑；
 * 未登记的返回裸 URL，交由浏览器 HTTP 缓存处理。
 */
export function resolveAssetUrl(url: string | null | undefined): string {
  if (!url) return ''
  const rec = liveRecord(url)
  return rec?.status === 'ok' ? rec.url : url
}

/** 加载成功：登记实际生效的 URL，并撤销此前的失败登记 */
export function markAssetOk(url: string | null | undefined, loadedUrl?: string): void {
  if (!url) return
  remember(url, { status: 'ok', url: loadedUrl || url, at: Date.now() })
}

/** 重试耗尽：登记为失效。窗口内后续挂载不再发请求 */
export function markAssetDead(url: string | null | undefined): void {
  if (!url) return
  remember(url, { status: 'dead', url, at: Date.now() })
}

/**
 * 构造**稳定**的重试 URL：`?hlretry=2`（不掺时间戳）。
 *
 * 稳定性是全部要点——见文件头 ②。保留换 query 而不是原样重发：失败可能来自中间
 * 代理缓存了一个坏响应，换 query 能绕开那一层，这是「重试」有意义的前提。
 */
export function assetRetryUrl(url: string, attempt: number): string {
  const sep = url.includes('?') ? '&' : '?'
  return `${url}${sep}hlretry=${attempt}`
}

/** 清空登记（测试 / 强制刷新用） */
export function resetAssetRegistry(): void {
  registry.clear()
}
