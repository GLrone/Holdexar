/* 区服名词条 —— 服务端表外的唯一一条。区服名中文单一来源是 GET /api/v1/regions 下发的
   RegionInfo.name（源自 CC_LIST），前端零硬编码；BD 不在 CC_LIST、服务端不下发，中文名由前端补。
   不变量：「孟加拉」不能改成 Intl.DisplayNames 的输出（那边给的是「孟加拉国」，改了就是显示变化）；
   英文侧主路径走 stores/regions.ts 的 regionDisplayName()，本表 en 值只是兜底。 */
const regions = {
  'regions.extra.BD': '孟加拉',
} as const

export default regions
