<script setup lang="ts">
/**
 * 可点选小标签（筛选 chip / 排序 chip / 时间范围 chip）。
 *
 * 四种造型（胶囊：`.bill-chip` / `.range-chip` 同款；方角：`.lib-sort` /
 * `.wl-filter` 同款）——收录进本组件后按 `shape` 取造型。
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
 * 内部按钮是透明块），语义上属于 `HlSegmented`。它暂未纳入，因为 HlSegmented 目前
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
