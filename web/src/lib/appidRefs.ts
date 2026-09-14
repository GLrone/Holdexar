/* AppID 引用解析（粘贴文本 → appid 清单）。
 *
 * 多入口共用：任务页的批量导入 / 收藏列表导入、监控池页的「添加监控条目」。
 * 同一份粘贴物在任何入口都应得出同一份清单——解析逻辑各写一份的话，
 * 「一边识别、一边丢」的口径分叉会悄悄发生。
 *
 * 支持的形态：
 * - Steam 商店 / SteamDB app 链接（含 ?query 与尾斜杠）
 * - 裸数字（空格 / 逗号 / 换行分隔）
 * - 收藏列表通道额外支持整段 JSON 数组（数字或对象数组，键 appid|appId|AppID|id）
 * 无法识别的非空段如实报出（invalid），不静默丢弃。
 */

/** Steam 商店 / SteamDB app 链接（含 ?query 与尾斜杠）→ appid；裸数字放行 */
const APP_URL_RE = /(?:store\.steampowered\.com|steamdb\.info|steamdb\.in)\/app\/(\d+)/i

export interface AppidRefs {
  appids: number[]
  invalid: string[]
}

/** 智能解析：逐段识别链接/裸 AppID；无法识别的非空段如实报出（不静默丢弃） */
export function parseAppRefs(text: string): AppidRefs {
  const appids: number[] = []
  const invalid: string[] = []
  const seen = new Set<number>()
  for (const raw of text.split(/[\s,，;；]+/)) {
    const s = raw.trim()
    if (!s) continue
    const m = APP_URL_RE.exec(s)
    const id = m ? Number(m[1]) : /^\d+$/.test(s) ? Number(s) : null
    if (id === null || !Number.isSafeInteger(id) || id <= 0) {
      invalid.push(s)
      continue
    }
    if (!seen.has(id)) {
      seen.add(id)
      appids.push(id)
    }
  }
  return { appids, invalid }
}

/** 从任意粘贴文本提取 AppID：JSON 数组（数字/对象）/ N 行裸数字 / 链接混排 */
export function parseFavoritesRefs(text: string): AppidRefs {
  const appids: number[] = []
  const invalid: string[] = []
  const seen = new Set<number>()

  const push = (v: unknown) => {
    const s = String(v ?? '').trim()
    const n = typeof v === 'number' ? v : /^\d+$/.test(s) ? Number(s) : NaN
    if (Number.isSafeInteger(n) && n > 0 && !seen.has(n)) {
      seen.add(n)
      appids.push(n)
    } else if (s !== '' && v !== null && v !== undefined) {
      invalid.push(s)
    }
  }

  const trimmed = text.trim()
  // 整体是 JSON 数组：逐元素提取（数字直取、对象按 appid|appId|id 键）
  if (trimmed.startsWith('[')) {
    try {
      const arr = JSON.parse(trimmed) as unknown[]
      for (const el of arr) {
        if (typeof el === 'object' && el !== null) {
          const o = el as Record<string, unknown>
          const key = ['appid', 'appId', 'AppID', 'id'].find((k) => o[k] !== undefined)
          push(key ? o[key] : o)
        } else {
          push(el)
        }
      }
      return { appids, invalid }
    } catch {
      /* JSON 断裂（截断复制等）→ 落回逐行扫描 */
    }
  }
  // 回落：按行/空白分隔裸数字与链接（与批量导入同规则）
  for (const raw of trimmed.split(/[\s,，;；]+/)) {
    const s = raw.trim()
    if (!s) continue
    const m = APP_URL_RE.exec(s)
    push(m ? m[1] : s)
  }
  return { appids, invalid }
}
