/** 区服元数据纯函数。区服列表本体由服务端 GET /api/v1/regions 下发（stores/regions.ts），前端零硬编码。 */
export interface RegionMeta {
  code: string
  name: string
  currency: string
}

/** 本地国旗素材（flagcdn 下载到 public/flags，离线可用） */
export function flagUrl(code: string): string {
  return `/flags/${code.toLowerCase()}.png`
}

/**
 * 窄列场景简写：去掉货币后缀后仍超过 4 字 → 前两字 + 后缀
 * （中文「哈萨克斯坦（KZT）」→「哈萨区」）。
 *
 * **后缀由调用方从词典传入**（`common.regionSuffix`：zh `区` / en 空串）——
 * 后缀不在此处写死：「取前两字 + 区」是**中文的排版习惯**，英文照搬会得到
 * 「Ka区」这种半中半英的东西。后缀为空串时本函数**不做任何简写、原样返回**
 * （英文的截断交给 CSS ellipsis：「Kazakh…」远好过「Ka区」）。空串在这里是
 * 有意义的取值，不是缺译，见 `locales/zh-CN/common.ts` 该词条的注。
 *
 * 顺带把去后缀的正则从 `（.+）$` 放宽为同时吃半角括号：英文侧产出的是
 * `Hong Kong (HKD)`（半角），不放宽就剥不掉、简写判断会拿到带货币后缀的整串。
 * 中文名用的是全角括号，行为不变。
 */
export function compactRegionName(name: string, suffix: string): string {
  const base = name.replace(/\s*[（(].+[)）]$/, '')
  return base.length > 4 && suffix ? base.slice(0, 2) + suffix : base
}

export function formatCnyFen(fen: number | null | undefined): string {
  if (fen === null || fen === undefined) return '—'
  return `¥${(fen / 100).toFixed(2)}`
}

export type SortKey = 'default' | 'smart' | 'rate' | 'diff' | 'discount' | 'top100' | 'new2026'
export type FilterMode = 'global' | 'cheaper' | 'highdiff'
