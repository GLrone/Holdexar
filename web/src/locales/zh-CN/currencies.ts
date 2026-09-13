/* ════════════════════════════════════════════════════════════════════
   期 7 数据层词条 —— 币种名（39 条）。

   它们原先写在 `api/currencies.ts` 的 `CURRENCIES[].zh` 里。搬进词典的理由
   不是"更好看"，而是**那 39 个中文字面量让 api/ 永远出不了 I18N_LEGACY_FILES**
   ——eslint 的 `no-hardcoded-cjk` 整目录豁免 `src/locales/**`（那是中文的正确
   落点），却如实扫 `api/**`。而那份存量清单的约定是"删空即表示迁移完成、
   红线全面生效"。搬进来之后这些名字同时拿到三件事：
     ① 落进 CJK 门禁的豁免目录；
     ② 受 check-i18n 的"中英逐条对齐"约束（原先只有中文，没有人看守）；
     ③ 译者可以直接改（en 侧现由 Intl 生成的初值仍可被人工覆盖）。

   ⚠️ **zh 侧取值与迁移前 `CURRENCIES[].zh` 逐字相同**（显示零变化），且
   **不要改成从 Intl.DisplayNames 现取**：实测 zh 侧有 2 条与本项目既有说法
   不一致 —— IDR 表为「印尼盾」而 Intl 为「印度尼西亚卢比」、ILS 表为
   「以色列谢克尔」而 Intl 为「以色列新谢克尔」。既有的短名写法（印尼 /
   阿联酋 / 韩国 / 英国 …）与 `CC_LIST` 的命名政策一致，以本表为准。

   对齐单一来源：`server/app/crawler/config.py` 的 CC_LIST（41 区 → 37 种唯一
   货币）；末尾 TRY / ARS 是服务端永久保留币种（当前无区服使用）。
   新增区服货币时：CC_LIST 加行 → `api/currencies.ts` 的 CURRENCIES 加一条
   （code / cc）→ 本表加 `currencies.name.<CODE>` 的中英各一条。
   ════════════════════════════════════════════════════════════════════ */
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
