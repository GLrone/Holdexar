/**
 * family 模块的分类色板 —— 全模块唯一出处。
 *
 * ## 固定色，不用主题 token
 *
 * 这组色的用途是「把 N 个成员 / N 个档位**一眼分开**」，要的是**彼此可区分**，
 * 不是「成功 / 警告」这类语义。取语义 token 反而会坏事：浅色主题下
 * `--success`(#6b9a00) 与 `--warning`(#d68910) 明度靠得很近，两个成员会糊成
 * 一块；而语义 token 还可能被别处改色，届时成员 1 的颜色会跟着变。
 *
 * 它们也不随主题变：取的都是中高明度，压在浅底（浅色主题）与深底（深色主题）
 * 上都能读。所以这是**刻意的定色**，不是「抄了深色取值忘了跟随主题」——
 * 后者是 `scripts/check-colors.mjs` 要拦的东西，本文件已在它的 ALLOW 清单登记。
 *
 * ## 单一出处
 *
 * 成员色 / 档位色 / 成长色各自只有一份定义，各 tab 一律从这里取——
 * 同一个成员在任何 tab 里都是同一个颜色。
 */

/** 分类色板的具名色（各处取用一律走名字，禁止再写裸十六进制） */
export const PALETTE = {
  teal: '#06cfbe',
  blue: '#54a0ff',
  amber: '#f59e0b',
  pink: '#ec4899',
  violet: '#8b5cf6',
  green: '#2ed573',
  sky: '#38bdf8',
  lilac: '#a78bfa',
  gold: '#fbbf24',
  orange: '#ff9f43',
  salmon: '#ff6b6b',
  iris: '#a29bfe',
  slate: '#94a3b8',
  /** 柠檬绿。取值恰与**深色主题**的 --success 相同——但这里是分类板的第三槽，
   *  不是「成功」语义，故仍按定色留在色板里（换语义 token 会让它在浅色主题下
   *  变成暗橄榄，与相邻的 amber 糊在一起）。 */
  lime: '#a4d007',
} as const

/** 成员色（按成员下标循环取用；顺序即「区分度优先」的排序） */
export const MEMBER_COLORS: readonly string[] = [
  PALETTE.blue,
  PALETTE.lime,
  PALETTE.amber,
  PALETTE.pink,
  PALETTE.violet,
  PALETTE.teal,
]

/** 贡献档位色（FamContrib 的堆叠条与图例） */
export const TIER_COLORS: readonly string[] = [
  PALETTE.teal,
  PALETTE.blue,
  PALETTE.lime,
  PALETTE.amber,
  PALETTE.violet,
]

/** 成长曲线色（FamGrowth） */
export const GROWTH_COLORS: readonly string[] = [
  PALETTE.teal,
  PALETTE.blue,
  PALETTE.orange,
  PALETTE.green,
  PALETTE.salmon,
  PALETTE.iris,
]

/** 按下标取成员色（越界回到开头） */
export function memberColor(index: number): string {
  return MEMBER_COLORS[index % MEMBER_COLORS.length]!
}
