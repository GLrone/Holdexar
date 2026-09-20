/**
 * 下拉选项统一出口（HlSelect 专用，收口规则见 web/eslint.config.mjs）。
 *
 * 地区/币种下拉的选项**只能**从这里取——旗帜由出口统一挂载，
 * 业务层零样板，"忘带旗"在结构上不可能：
 *
 *   <HlSelect v-model="region" :options="regionSelectOptions()" />
 *
 * eslint 已在视图层封死 `flagUrl` / `currencyFlagUrl` 直连（见
 * web/eslint.config.mjs 收口规则），新代码只能走本出口或
 * `<RegionFlag>` / `<CurrencyFlag>` 组件。
 */
import type { HlSelectOption } from '@/components/ui/HlSelect.vue'

import { currencyFlagUrl, currencyName, CURRENCIES } from '@/api/currencies'
import { flagUrl } from '@/api/regions'
import { useRegionsStore } from '@/stores/regions'

/**
 * 地区下拉选项。
 *
 * @param codes 可选区码子集（如价格矩阵 `Object.keys(priceMatrix)`，
 *   大小写不限、保持传入顺序）；缺省 = 全量区服（服务端下发顺序）
 * @param opts.withCurrency label 追加货币后缀「名（XXX）」，详情页场景用
 *
 * value 一律小写区码（history API / 筛选 store / priceMatrix 键的既有约定）；
 * 名称查不到时回退区码本身。需响应式时调用侧包 computed（出口纯函数不持状态）。
 */
export function regionSelectOptions(
  codes?: string[],
  opts?: { withCurrency?: boolean },
): HlSelectOption[] {
  const regions = useRegionsStore()
  const list = codes
    ? codes.map((code) => regions.metaByCode(code) ?? { code, name: code.toUpperCase(), currency: '' })
    : regions.metas
  return list.map((r) => ({
    value: r.code.toLowerCase(),
    label: opts?.withCurrency && r.currency ? `${r.name}（${r.currency}）` : r.name,
    flag: flagUrl(r.code),
  }))
}

/**
 * 币种下拉选项。
 *
 * @param codes 可选币种子集（如追踪币种白名单）；缺省 = 全量币种表
 * @param opts.withCode 名称后附 ISO 代号（弱化等宽小字），追踪多选场景用
 *
 * value = ISO 代号（USD / JPY …），label = **当前语言**的币种名。
 *
 * ⚠️ 与 `regionSelectOptions` 不同的是：这里的 label 会随语言变，所以本出口
 * **不是纯函数**了（`currencyName` 走 `t()`）。调用侧若要跟随切换语言重算，
 * 照旧包一层 `computed(() => currencySelectOptions())` 即可——`t()` 在调用期
 * 读 locale store，computed 求值时即建立依赖。
 */
export function currencySelectOptions(
  codes?: string[],
  opts?: { withCode?: boolean },
): HlSelectOption[] {
  const list = codes ? CURRENCIES.filter((c) => codes.includes(c.code)) : CURRENCIES
  return list.map((c) => ({
    value: c.code,
    label: currencyName(c.code),
    flag: currencyFlagUrl(c.code),
    meta: opts?.withCode ? c.code : undefined,
  }))
}
