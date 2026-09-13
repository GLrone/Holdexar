/* ════════════════════════════════════════════════════════════════════
   English 词条 —— 币种名（39 条），与 `zh-CN/currencies.ts` 逐条对齐。

   初值取自 `Intl.DisplayNames(['en-US'], { type: 'currency' })` 的实测输出
   （39 个 code 全部被 Intl 认识，无一回退）。**此后以本表为准，不再回读 Intl**
   ——词典的价值就在于它是人可以改的那一份；若哪天要改成 "RMB" 这类项目自己的
   说法，改这里即可，不必去动 `api/currencies.ts`。

   注意 CRC 的 "Costa Rican Colón" 带非 ASCII 的 ó，是 Intl 的正确拼法，勿"修正"。
   ════════════════════════════════════════════════════════════════════ */
const currencies = {
  // ── 按 CC_LIST 首次出现顺序；末尾两条为服务端预留币种 ──
  'currencies.name.CNY': 'Chinese Yuan',
  'currencies.name.RUB': 'Russian Ruble',
  'currencies.name.KZT': 'Kazakhstani Tenge',
  'currencies.name.UAH': 'Ukrainian Hryvnia',
  'currencies.name.USD': 'US Dollar',
  'currencies.name.VND': 'Vietnamese Dong',
  'currencies.name.IDR': 'Indonesian Rupiah',
  'currencies.name.INR': 'Indian Rupee',
  'currencies.name.BRL': 'Brazilian Real',
  'currencies.name.CLP': 'Chilean Peso',
  'currencies.name.JPY': 'Japanese Yen',
  'currencies.name.HKD': 'Hong Kong Dollar',
  'currencies.name.PHP': 'Philippine Peso',
  'currencies.name.TWD': 'New Taiwan Dollar',
  'currencies.name.KWD': 'Kuwaiti Dinar',
  'currencies.name.SAR': 'Saudi Riyal',
  'currencies.name.ZAR': 'South African Rand',
  'currencies.name.QAR': 'Qatari Riyal',
  'currencies.name.MYR': 'Malaysian Ringgit',
  'currencies.name.THB': 'Thai Baht',
  'currencies.name.PEN': 'Peruvian Sol',
  'currencies.name.MXN': 'Mexican Peso',
  'currencies.name.SGD': 'Singapore Dollar',
  'currencies.name.AED': 'United Arab Emirates Dirham',
  'currencies.name.UYU': 'Uruguayan Peso',
  'currencies.name.COP': 'Colombian Peso',
  'currencies.name.KRW': 'South Korean Won',
  'currencies.name.NZD': 'New Zealand Dollar',
  'currencies.name.PLN': 'Polish Zloty',
  'currencies.name.CRC': 'Costa Rican Colón',
  'currencies.name.CAD': 'Canadian Dollar',
  'currencies.name.AUD': 'Australian Dollar',
  'currencies.name.EUR': 'Euro',
  'currencies.name.GBP': 'British Pound',
  'currencies.name.NOK': 'Norwegian Krone',
  'currencies.name.ILS': 'Israeli New Shekel',
  'currencies.name.CHF': 'Swiss Franc',

  // ── 预留币种（当前无区服使用，服务端仍抓取落库） ──
  'currencies.name.TRY': 'Turkish Lira',
  'currencies.name.ARS': 'Argentine Peso',
}

export default currencies
