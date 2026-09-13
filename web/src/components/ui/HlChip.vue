<script setup lang="ts">
/**
 * 可点选小标签（筛选 chip / 排序 chip / 时间范围 chip）。
 *
 * **为什么要有这个组件**：收敛前全项目有 4 处手写实现，其中两对是**逐字相同**的
 * 两份副本，只差类名——
 *
 * | 造型 | 副本一 | 副本二 |
 * |---|---|---|
 * | 胶囊（999px 圆角） | `.bill-chip`（`tabs-shared.css`，FamLicense / toolbox 用） | `.range-chip`（`PriceTrendDrawer` / `game-detail` 的走势区间） |
 * | 方角（5px 圆角） | `.lib-sort`（`tabs-shared.css`，FamBuy / FamLib 用） | `.wl-filter`（`FamWish`） |
 *
 * 后两份的声明**完全一致**（`font-size:11px; padding:4px 10px; border-radius:5px` +
 * 同一套 border/background/color/hover/is-on），改一处必漏另一处。
 *
 * **两个维度都按「外观」而非「用途」命名**：
 *   · `shape` 是造型（胶囊 vs 方角）。不叫 `variant` / `type`——那会诱导后来者
 *     按用途再加一档（"这是筛选、那是排序"），而造型与用途在本项目里并不对应
 *     （同样是筛选，toolbox 用胶囊、FamWish 用方角）。
 *   · `tone` 是**静息态**配色：`neutral` 是中性底 + 中性字，`accent` 是常驻强调色
 *     （走势图那两处 chip 与图表同处一张卡，静息态就跟图表一个色调）。
 *     悬停态与选中态两档一致——差异只在"没被选中、也没悬停"时。
 *
 * 不在这里的：`FamBuy` 的 `.buy-view-toggle` 是**内嵌分段**（容器自带底与内距、
 * 内部按钮是透明块），语义上属于 `HlSegmented`。它暂未迁移，因为 HlSegmented 目前
 * 只接受纯文本 `options`、没有图标插槽，而它那两个按钮是「图标 + 文字」。
 * 已登记为例外。
 */
withDefaults(
  defineProps<{
    /** pill=胶囊（12px 字 / 999px 圆角） · soft=方角（11px 字 / 5px 圆角） */
    shape?: 'pill' | 'soft'
    /** 静息态配色。neutral=中性 · accent=常驻强调色 */
    tone?: 'neutral' | 'accent'
    /** 选中态。由调用方判定：`:on="active === k"`，与 `HlSegmented` 的 v-model 取舍不同——
     *  这里的选中集合常由外部条件决定（如「排序字段 === 本项 且 未开独占筛选」） */
    on?: boolean
  }>(),
  { shape: 'pill', tone: 'neutral', on: false },
)
</script>

<template>
  <button
    type="button"
    class="hl-chip"
    :class="[`hl-chip--${shape}`, `hl-chip--${tone}`, { 'is-on': on }]"
  >
    <slot />
  </button>
</template>
