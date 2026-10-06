/* 币种名词条（39 条）。zh 侧取值以本表为准，不要改成从 Intl.DisplayNames 现取
   （IDR / ILS 两条与 Intl 输出不同，既有短名与 CC_LIST 命名政策一致）。
   对齐单一来源：server/app/crawler/config.py 的 CC_LIST；末尾 TRY / ARS 为服务端永久保留币种。
   新增区服货币：CC_LIST 加行 → api/currencies.ts 加一条 → 本表加中英各一条。 */
const currencies = {
  // ── 按 CC_LIST 首次出现顺序；末尾两条为服务端预留币种 ──
  'currencies.name.CNY': '人民币',
  'currencies.name.RUB': '俄罗斯卢布',
  'currencies.name.KZT': '哈萨克斯坦坚戈',
  'currencies.name.UAH': '乌克兰格里夫纳',
  'currencies.name.USD': '美元',
  'currencies.name.VND': '越南盾',
  'currencies.name.IDR': '印尼盾',
  'currencies.name.INR': '印度卢比',
  'currencies.name.BRL': '巴西雷亚尔',
  'currencies.name.CLP': '智利比索',
  'currencies.name.JPY': '日元',
  'currencies.name.HKD': '港元',
  'currencies.name.PHP': '菲律宾比索',
  'currencies.name.TWD': '新台币',
  'currencies.name.KWD': '科威特第纳尔',
  'currencies.name.SAR': '沙特里亚尔',
  'currencies.name.ZAR': '南非兰特',
  'currencies.name.QAR': '卡塔尔里亚尔',
  'currencies.name.MYR': '马来西亚林吉特',
  'currencies.name.THB': '泰铢',
  'currencies.name.PEN': '秘鲁索尔',
  'currencies.name.MXN': '墨西哥比索',
  'currencies.name.SGD': '新加坡元',
  'currencies.name.AED': '阿联酋迪拉姆',
  'currencies.name.UYU': '乌拉圭比索',
  'currencies.name.COP': '哥伦比亚比索',
  'currencies.name.KRW': '韩元',
  'currencies.name.NZD': '新西兰元',
  'currencies.name.PLN': '波兰兹罗提',
  'currencies.name.CRC': '哥斯达黎加科朗',
  'currencies.name.CAD': '加拿大元',
  'currencies.name.AUD': '澳大利亚元',
  'currencies.name.EUR': '欧元',
  'currencies.name.GBP': '英镑',
  'currencies.name.NOK': '挪威克朗',
  'currencies.name.ILS': '以色列谢克尔',
  'currencies.name.CHF': '瑞士法郎',

  // ── 预留币种（当前无区服使用，服务端仍抓取落库） ──
  'currencies.name.TRY': '土耳其里拉',
  'currencies.name.ARS': '阿根廷比索',
} as const

export default currencies
