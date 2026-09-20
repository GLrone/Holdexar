import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { regionsApi, type RegionInfo } from '@/api/client'
import type { RegionMeta } from '@/api/regions'
import { useI18n, type MessageKey } from '@/locales'
import { useLocaleStore } from '@/stores/locale'

/**
 * 区服元数据（服务端单一来源）。
 * GET /api/v1/regions 下发全量列表；启用集也在这里（「我」页配置，爬取严格按此执行）。
 * 应用启动时（main.ts）阻塞加载一次，之后所有组件同步可用。
 *
 * 区名双语见下方 `regionDisplayName()`。
 */
export const useRegionsStore = defineStore('regions', () => {
  const { t } = useI18n()
  const localeStore = useLocaleStore()
  const list = ref<RegionInfo[]>([])
  const loaded = ref(false)

  /** 启用爬取的区（小写 code）；空列表 = 未启用任何区 */
  const enabledCodes = computed(() =>
    list.value.filter((r) => r.enabled).map((r) => r.code.toLowerCase()),
  )

  /** 服务端下发的区名带货币后缀（`中国香港（HKD）`），拆出来供英文侧重拼 */
  const stripCurrency = (name: string) => name.replace(/\s*[（(].+[)）]$/, '')

  /**
   * 英文侧区名的覆盖表。
   *
   * 中文侧一律用服务端下发的名字（那是中文的单一来源，前端零硬编码），
   * **只有非中文**才走这里 + `Intl.DisplayNames`。四条覆盖都不是审美偏好：
   *
   * - `PK` / `PT`：CC_LIST 里这两条**不是国家**，是聚合区（南亚容灾区 / 欧元区）。
   *   `Intl.DisplayNames(['en-US'],{type:'region'})` 分别给出 "Pakistan" 与
   *   "Portugal"——语义直接错，会把「南亚」显示成「巴基斯坦」。
   * - `HK`：Intl 给 "Hong Kong SAR China"。CC_LIST 顶部有明文政策
   *   （中国香港、中国台湾为固有命名，不得改为变体），对应到英文就是取惯用的
   *   短名 "Hong Kong"。
   * - `TR`：Intl 给 "Türkiye"（2022 年起的官方名）。本项目对齐 Steam 的用词，
   *   Steam 用的是 "Turkey"。
   *
   * 其余 37 个区码全部取 Intl，不再逐条抄写——手工维护一张 41 条的英文表，
   * 唯一的产物就是与 CC_LIST 的漂移。
   */
  const EN_REGION_OVERRIDES: Record<string, string> = {
    PK: 'South Asia',
    PT: 'Eurozone',
    HK: 'Hong Kong',
    TR: 'Turkey',
  }

  /**
   * `Intl.DisplayNames` 缓存（按语言）。
   *
   * 构造开销远大于查询，而区名在列表里逐行取、每次渲染都取，故按语言记忆、
   * 模块级存活（与 `locales/format.ts` 的 num/date 缓存同一思路）。
   */
  const dnCache = new Map<string, Intl.DisplayNames>()
  function displayNames(tag: string): Intl.DisplayNames {
    let d = dnCache.get(tag)
    if (!d) {
      d = new Intl.DisplayNames([tag], { type: 'region' })
      dnCache.set(tag, d)
    }
    return d
  }

  /**
   * `Intl.DisplayNames` 取区域名，**任何输入都返回 `string | null`、绝不抛**。
   *
   * 三处行为（`Intl.DisplayNames` 的实际表现）：
   *   `of('')` / `of('U')` / `of('USA1')` / `of('U$')` / `of('ZZZ')` → 抛 RangeError；
   *   `of('cn')` → 返回 `"cn"`（小写既不报错也不翻译，**原样回显**）；
   *   `of('CN')` → `"China"`。
   * 所以：先 toUpperCase 再查，并 try/catch；取回的值若与输入相同（回显）也当作
   * 没取到 —— 回显不是名字。
   */
  function intlRegionName(tag: string, upperCode: string): string | null {
    try {
      const v = displayNames(tag).of(upperCode)
      return v && v !== upperCode ? v : null
    } catch {
      return null
    }
  }

  /**
   * 区名（当前语言）。
   *
   * 中文：服务端下发的名字**原样**（含货币后缀）。
   * 非中文：覆盖表 → `Intl.DisplayNames` → 服务端名字，再补上货币后缀
   *   （半角括号，与英文排版一致；中文侧的全角括号不动）。
   *
   * ⚠️ **必须保持「任何输入都返回字符串、绝不抛」**——调用点全都不做 try/catch。
   * `Intl.DisplayNames` 对区域类型有两处脾气，都要在这里挡住：
   *   ① 畸形输入抛 `RangeError`（空串、2 位、4 位、含符号都抛）；
   *   ② **小写输入不报错也不翻译，原样回显**（`of('cn')` → `"cn"`），
   *      所以必须先 `toUpperCase()`（对比：币种类型接受小写，见
   *      `api/currencies.ts` 的注）；
   *   ③ 未知但合形的码同样抛 `RangeError`（`of('ZZZ')`），故得 try/catch，
   *      形状校验不够。
   */
  function regionDisplayName(code: string, serverName: string, currency: string): string {
    if (localeStore.locale === 'zh-CN') return serverName
    const upper = code.toUpperCase()
    const base =
      EN_REGION_OVERRIDES[upper] ?? intlRegionName(localeStore.locale, upper) ?? stripCurrency(serverName)
    return currency ? `${base} (${currency})` : base
  }

  /** 供 priceMatrix/下拉等场景使用的元数据（保持服务端下发顺序） */
  const metas = computed<RegionMeta[]>(() =>
    list.value.map((r) => ({
      code: r.code.toUpperCase(),
      // 列表 / 下拉 / 价格行都直接显示 metas[].name，故双语化落在这里一处即可——
      // regionName() 只是它的按码取用，不再各写一份。
      name: regionDisplayName(r.code.toUpperCase(), r.name, r.currency),
      currency: r.currency,
    })),
  )

  function metaByCode(code: string): RegionMeta | undefined {
    const upper = code.toUpperCase()
    return metas.value.find((r) => r.code === upper)
  }

  /**
   * 区服表外的特殊区码 → 词条 key。
   *
   * 只有 BD（捆绑包南亚容灾区）：它不在 CC_LIST 里，服务端不下发，中文名只能由
   * 前端补。存 key 不存文案是冻结陷阱的标准解法；写成 `Record<string, MessageKey>`
   * 而不是拼 `t('regions.extra.' + code)`，是为了让 key 以**字面量**出现——
   * 拼字符串的话写错了 `MessageKey` 就管不住，运行时静默显示 key 原文。
   */
  const EXTRA_REGION_KEY: Record<string, MessageKey> = { BD: 'regions.extra.BD' }

  function regionName(code: string): string {
    const upper = code.toUpperCase()
    const named = metaByCode(upper)?.name
    if (named) return named
    const extra = EXTRA_REGION_KEY[upper]
    return extra ? t(extra) : upper
  }

  async function load(force = false) {
    if (loaded.value && !force) return
    try {
      const data = await regionsApi.list()
      list.value = data.regions
      ownedRegions.value = data.ownedRegions ?? null
      loaded.value = true
    } catch {
      /* 静默：后端不可达时组件按空列表渲染 */
    }
  }

  async function setEnabled(codes: string[] | null) {
    const data = await regionsApi.setEnabled(codes)
    list.value = data.regions
  }

  /** 已购游戏抓取区：null = 跟随启用集；列表 = 自定义子集 */
  const ownedRegions = ref<string[] | null>(null)

  async function setOwned(codes: string[] | null) {
    const data = await regionsApi.setOwned(codes)
    ownedRegions.value = data.ownedRegions ?? null
  }

  return {
    list,
    loaded,
    enabledCodes,
    metas,
    metaByCode,
    regionName,
    ownedRegions,
    load,
    setEnabled,
    setOwned,
  }
})
