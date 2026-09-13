/* ════════════════════════════════════════════════════════════════════
   English 词条 —— 区服名里服务端表外的那一条（与 `zh-CN/regions.ts` 对齐）。

   值取自 `Intl.DisplayNames(['en-US'],{type:'region'}).of('BD')` 的实测输出。
   注意 `stores/regions.ts` 的英文区名主路径是 Intl（覆盖服务端下发的全部区码），
   本词条只在 Intl 不可用/取不到时兜底，且供 check-i18n 的中英对齐判据使用。
   ════════════════════════════════════════════════════════════════════ */
const regions = {
  'regions.extra.BD': 'Bangladesh',
}

export default regions
