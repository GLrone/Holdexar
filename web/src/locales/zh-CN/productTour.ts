/* ════════════════════════════════════════════════════════════════════
   productTour 词条 —— 新手引导浮层（components/ProductTour.vue）。

   **冻结陷阱（本文件存在的理由）**：ProductTour 的步骤表 `TOUR` 是写在
   `<script setup>` 里的一张常量表，如果在表里直接存 `title: '欢迎来到…'`
   这样的译文，字符串会在组件创建那一刻求值一次——切语言时引导文案原地不动
   （`t()` 的响应式只在**渲染期**调用才成立）。所以表里只存 **key**
   （`titleKey` / `textKey` / `emphKey` / `hintKey`），渲染期再 `t(...)`。
   `scripts/check-i18n.mjs` 的判据 ③ 专门盯这类 `xxxKey: 'a.b.c'` 常量，
   拼错同样会 exit 1。

   `{app}` 是品牌名参数，值由组件侧从 `@/appInfo` 的 `APP_NAME` 传入
   （表里存的是常量引用，不是译文，故不冻结）——品牌名改名时只动 appInfo。

   ⚠️ ProductTour 的 `target` 选择器里那四个 `data-section` 值是**契约**、
   不是文案：`crawl.section.bulkImport` / `alerts.section.rules` /
   `proxies.section.clash` / `settings.section.steamAccount`。它们既不出现在
   本文件，也不该建词条——DOM 锚点与语言无关，建了反而多一个会漂移的来源。
   四个视图侧写的是同一串字符串。

   文案口径（写给没接触过比价工具的人）：
   · 只教主线（加游戏 → 看价格 → 设提醒），界面清单、进阶玩法（汇率 /
     工具箱 / 邮件通知等）一律不进导览，留给用户自行探索；
   · 直连是抓取的标准形态——代理只以「网络不畅时的改善手段」出现
     （价格迟迟取不到 / 登录窗打不开），不写成任何功能的前置条件；
   · 引导里点名的界面标签逐字取自各页词典：游戏商店 → nav.library ·
     代理管理 → nav.proxies · 我 → nav.me · 导入监控 → crawl.bulk.import ·
     价格 ≤ 目标 / 折扣 ≥ 目标 / 跌破历史最低 → alerts.rules.optionPrice
     / optionPct / optionHistoricLow · 登录并自动获取 → settings.steam.autoFetch ·
     新手教程 → settings.section.tour。
   引导底部的「上一步 / 下一步 / 完成」复用 common.prev / common.next
   / common.finish（与 HlStepper 同词条，非本模块自有文案）。
   ════════════════════════════════════════════════════════════════════ */

const productTour = {
  /* ── 开场卡（无聚光靶点）── */
  'productTour.intro.title': '欢迎来到 {app}',
  'productTour.intro.p1':
    '这是一款 Steam 多区比价工具：同一款游戏在全球 40 多个区的价格放在一起看，哪里便宜一目了然。',
  'productTour.intro.p2':
    '接下来用一分钟走一遍主线——添加游戏、看价格、设提醒，三步就能用起来。',
  'productTour.intro.p3':
    '中途随时可以点「跳过导览」；以后想重看，到「关于」页点 {app} logo 就行。',

  /* ── 第 1 步 · 商店页（比价结果看什么）── */
  'productTour.stepData.title': '第 1 步 · 比价结果都在这里',
  'productTour.stepData.p1':
    '这是「游戏商店」：每张卡片是一款游戏，能看到它在全球各区的现价、折扣和历史最低价。',
  'productTour.stepData.p2':
    '刚装好时这里是空的，很正常——价格要由系统自动去 Steam 取回来，先告诉它你关心哪些游戏，数据就会进来。',
  'productTour.stepData.emph':
    '不用先配置任何东西，下一步就教你把游戏加进来。',
  'productTour.stepData.hint':
    '加完游戏回到这里，卡片会自动出现。',

  /* ── 第 2 步 · 导入游戏（主线动作）── */
  'productTour.stepImport.title': '第 2 步 · 把想比价的游戏加进来',
  'productTour.stepImport.p1':
    '在 Steam 商店打开一款游戏，复制浏览器地址栏里的链接。',
  'productTour.stepImport.p2':
    '粘贴到这个输入框（一次可粘多条，一行一条），点「导入监控」。',
  'productTour.stepImport.emph':
    '完成。之后价格每 6 小时自动更新一轮，不用你盯着。',
  'productTour.stepImport.hint':
    '回到「游戏商店」页，等第一轮价格更新完卡片就会出现；想长期盯哪款，在卡片上点一下星标。',

  /* ── 第 3 步 · 价格提醒（主线动作）── */
  'productTour.stepRules.title': '第 3 步 · 降价了让它提醒你',
  'productTour.stepRules.p1':
    '搜索并选中一款游戏，然后挑一个条件，比如「价格 ≤ 目标」——填一个你愿意出手的价；也可以选「折扣 ≥ 目标」或「跌破历史最低」。',
  'productTour.stepRules.p2':
    '点「添加」就完成了。之后每轮价格更新都会自动核对，一旦达标就会通知你。',
  'productTour.stepRules.emph':
    '提醒默认发在应用内，不用额外配置。',

  /* ── 可选分支 · 代理（网络不畅时的改善手段）── */
  'productTour.stepProxy.title': '可选 · 网络不畅时再配代理',
  'productTour.stepProxy.emph':
    '这一步可以整个跳过——系统默认直连就能取到价格，前面三步不依赖它。',
  'productTour.stepProxy.p1':
    '只有当价格迟迟取不到、或登录窗口打不开时，才值得到「代理管理」页配置代理。',
  'productTour.stepProxy.p2':
    '它支持粘贴订阅链接，按页面提示操作即可；具体细节等真正需要时再看这页的说明也不迟。',

  /* ── 可选分支 · Steam 账号绑定（自动化增强）── */
  'productTour.stepBind.title': '可选 · 绑定 Steam 账号',
  'productTour.stepBind.emph':
    '不绑定也完全不影响前面的功能——这只影响自动化程度。',
  'productTour.stepBind.p1':
    '绑定后，你的愿望单和已购游戏会自动进来持续跟踪比价，还能看到钱包余额。',
  'productTour.stepBind.p2':
    '点「登录并自动获取」按提示登录即可；如果登录窗口一直打不开，先去上一步配置代理。',
  'productTour.stepBind.hint':
    '找不到自动获取按钮时，这张卡片里有手动引导。',

  /* ── 收尾卡（无聚光靶点）── */
  'productTour.done.title': '完成！主线就这三步',
  'productTour.done.emph':
    '添加游戏 → 看价格 → 设提醒——接下来系统自动运行。',
  'productTour.done.p1': '价格每 6 小时自动更新、到价自动通知，什么都不用盯。',
  'productTour.done.p2':
    '其他功能（汇率、工具箱、我的家庭组等）不急着了解，用到了再去探索。',
  'productTour.done.p3':
    '想重看本导览：到「关于」页点 {app} logo，或到「我」页「新手教程」分节。',

  /* ── 浮层自身的控件 ── */
  'productTour.action.skip': '跳过导览',
  /* 步骤进度（气泡左上角胶囊）。分隔符 `/` 是中性符号，整条进词条不拼接。 */
  'productTour.progress': '{current} / {total}',
} as const

export default productTour
