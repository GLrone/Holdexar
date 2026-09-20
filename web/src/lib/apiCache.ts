/**
 * 极简请求结果复用（模块级 TTL memo + 并发合并）。
 *
 * 缓存键与生命期放**模块作用域**而不是 Pinia store：store 只覆盖「已进 store 的
 * 接口」，另有一批**组件自己发的 GET**——宿主面板用 `v-if` 切换（bills 三个 tab、
 * family 十个 tab），组件一离开就被销毁，组件内的 `ref` 连同数据一起没了；切回来时
 * `onMounted` 再跑一次 `load()`，同一份响应被反复下载。把守卫写进每个组件
 * （`if (!payload.value) load()`）解决不了——`payload` 已随组件销毁。
 * 模块作用域不参与响应式（这不是驱动界面的业务状态，只是「同一份响应别下第二遍」），
 * 生命周期跟随页面会话。
 *
 * 两层复用：
 * ① **TTL memo**——窗口内命中直接回，不打网络；
 * ② **in-flight 合并**——同一 key 正在飞时复用同一个 Promise。这一层比 TTL 更重要：
 *    两个 tab 同时挂载、或用户连点两次刷新时，没有它会**并发**发出同样的请求，
 *    而后端那类要现拉 Steam 的接口是 2~3s 级，并发只会互相拖慢。
 *
 * 与「图片资源复用」的关系：图片走的是浏览器 HTTP 缓存（本项目图片全是 Steam CDN 直链，
 * 不经后端代理），不需要本模块介入；这里解决的是 XHR/JSON 那一类。图片侧另有一层
 * 浏览器做不到的事——「这张已知失效」要跨组件实例记忆、重试 URL 要稳定——在 `assetCache.ts`。
 */

const store = new Map<string, { at: number; value: unknown }>()
const inflight = new Map<string, Promise<unknown>>()

/** 默认复用窗口。取 60s：足够吸收「切走再切回」「连点 tab」这类密集重复，
 *  又短到用户手动改完数据回头看一眼时不会读到明显过期的内容。 */
export const DEFAULT_TTL_MS = 60_000

export interface CachedGetOptions {
  /** 复用窗口（毫秒），默认 {@link DEFAULT_TTL_MS} */
  ttlMs?: number
  /** 跳过复用、强制重取（手动刷新按钮用）。取到后照常写入缓存 */
  force?: boolean
}

/**
 * 取一份可复用的 GET 结果。
 *
 * @param key 缓存键。**必须把所有影响响应的参数编进去**（id / 筛选 / 搜索词），
 *            否则筛选条件不同的两次请求会互相串数据。
 * @param fetcher 真正发请求的函数；只在未命中时调用。
 */
export function cachedGet<T>(
  key: string,
  fetcher: () => Promise<T>,
  opts: CachedGetOptions = {},
): Promise<T> {
  const { ttlMs = DEFAULT_TTL_MS, force = false } = opts

  if (!force) {
    const hit = store.get(key)
    if (hit && Date.now() - hit.at < ttlMs) return Promise.resolve(hit.value as T)
    const pending = inflight.get(key)
    if (pending) return pending as Promise<T>
  }

  const p = fetcher()
    .then((value) => {
      // 时间戳记在**响应到达**时刻，而不是发起时刻：否则一次 3s 的慢请求
      // 会把它的复用窗口白白吃掉 3s。
      store.set(key, { at: Date.now(), value })
      return value
    })
    .finally(() => {
      inflight.delete(key)
    })

  inflight.set(key, p)
  return p
}

/** 让某个 key 立即失效（数据被改动后调用） */
export function invalidate(key: string): void {
  store.delete(key)
}

/** 按前缀失效（如某个 importId 下的全部账单查询） */
export function invalidatePrefix(prefix: string): void {
  for (const k of store.keys()) if (k.startsWith(prefix)) store.delete(k)
}

/** 清空全部（退出登录 / 切换数据集时调用） */
export function invalidateAll(): void {
  store.clear()
}
