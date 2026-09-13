/* ════════════════════════════════════════════════════════════════════
   期 7 数据层词条 —— 区服名里**服务端表外**的那一条。

   绝大多数区服名不在这里，也不该在这里：`GET /api/v1/regions` 下发的
   `RegionInfo.name`（源自 `server/app/crawler/config.py` 的 CC_LIST）就是中文的
   单一来源，前端零硬编码——这正是 `stores/regions.ts` 的既有设计。

   BD 是唯一的例外：它是捆绑包南亚容灾区，**不在 CC_LIST 里**，服务端不会下发，
   于是中文名只能由前端补。原先它写成 `stores/regions.ts` 里的
   `EXTRA_REGION_NAMES = { BD: '孟加拉' }`；搬进词典的理由与币种名同源——
   `stores/**` 不在 CJK 规则的词典豁免目录内，那个字面量会把该文件永远钉在
   I18N_LEGACY_FILES 上。

   ⚠️ 值「孟加拉」**不能改成 Intl.DisplayNames 的输出**：实测
   `Intl.DisplayNames(['zh-CN'],{type:'region'}).of('BD')` 给的是「孟加拉国」，
   比本项目的既有说法多一个字，改了就是显示变化。这里沿用短名。

   英文侧 counterpart 走 Intl（`Bangladesh`），见 `stores/regions.ts` 的
   `regionDisplayName()`——所以本词条的 en 值只是该出口的兜底，不是主路径。
   ════════════════════════════════════════════════════════════════════ */
const regions = {
  'regions.extra.BD': '孟加拉',
} as const

export default regions
