/* 版本列表的展示口径——游戏卡 GPW 弹窗 / 详情页价格区块 / 走势抽屉三处共用。

   标准版判据「无后缀且非 gold」与后端 history 的 standard、db_writer 的
   is_standard 同口径。**同一款游戏的标准版可能横跨多个 sub 代际**（Steam 改包
   内容就换 sub_id），请求侧缺省 subId 拿到的正是这些代际的并集序列——它们代表
   同一个「标准版」，因此不在列表里各占一行（否则「标准版」会出现两行，一行还
   得挂上 #subId 才分得清）。同理，非标准版的同签名多 sub 仍用 #subId 消歧。 */

import type { HlSelectOption } from '@/components/ui/HlSelect.vue'
import type { MessageKey } from '@/locales'

/** 版本元信息（history 与 /versions 两处响应共有的三字段） */
export interface VersionLike {
  subId: number | null
  suffix: string | null
  isGold: boolean
}

/** 可选条目：subId 缺失的行选不了，排除在列表之外 */
export interface SelectableVersion extends VersionLike {
  subId: number
}

export function isStandardVersion(v: VersionLike): boolean {
  return !v.suffix && !v.isGold
}

/** 非标准版且 subId 可用的条目 */
export function selectableVariants(list: VersionLike[]): SelectableVersion[] {
  return list.filter(
    (v): v is SelectableVersion => !isStandardVersion(v) && typeof v.subId === 'number',
  )
}

/** subId → 展示标签（同签名多 sub 加 #subId 消歧） */
export function versionLabels(
  list: VersionLike[],
  t: (key: MessageKey) => string,
): Map<number, string> {
  const variants = selectableVariants(list)
  const sigCount = new Map<string, number>()
  for (const v of variants) {
    const sig = `${v.suffix ?? ''}|${v.isGold ? 1 : 0}`
    sigCount.set(sig, (sigCount.get(sig) ?? 0) + 1)
  }
  const labels = new Map<number, string>()
  for (const v of variants) {
    const sig = `${v.suffix ?? ''}|${v.isGold ? 1 : 0}`
    const base = v.suffix || (v.isGold ? t('trendDrawer.version.gold') : `#${v.subId}`)
    labels.set(v.subId, (sigCount.get(sig) ?? 0) > 1 ? `${base} #${v.subId}` : base)
  }
  return labels
}

/** 版本下拉选项：首项固定「标准版」（value 0 = 请求侧缺省 subId），其后为各变体 */
export function versionSelectOptions(
  list: VersionLike[],
  t: (key: MessageKey) => string,
): HlSelectOption[] {
  const labels = versionLabels(list, t)
  return [
    { value: 0, label: t('trendDrawer.version.standard') },
    ...selectableVariants(list).map((v) => ({
      value: v.subId,
      label: labels.get(v.subId) ?? `#${v.subId}`,
    })),
  ]
}