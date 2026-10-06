/* English 词条 —— 区服名里服务端表外的那一条（与 zh-CN/regions.ts 对齐）。
   值取自 Intl.DisplayNames(['en-US'],{type:'region'}).of('BD')。不变量：stores/regions.ts 的英文区名主路径是 Intl，
   本词条只在 Intl 取不到时兜底，并供 check-i18n 的中英对齐判据使用。 */
const regions = {
  'regions.extra.BD': 'Bangladesh',
}

export default regions
