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

   ⚠️ ProductTour 的 `target` 选择器里的 `data-section` 值是**契约**、
   不是文案：`pool.section.items` / `settings.section.steamAccount`。它们既不
   出现在本文件，也不该建词条——DOM 锚点与语言无关，建了反而多一个会漂移的
   来源。视图侧写的是同一串字符串。`data-tour` 值（nav-search / card-price /
   lib-empty）同属契约，不需要词条。

   文案口径（按用户主链教学）：
   · 主链 = 找游戏 → 看各区价格 → 关注 → 自动更新 → 到价提醒；引导里点名的
     界面标签逐字取自导航词典（找游戏 / 我的关注 / 价格提醒 / 设置）；
   · 价格数据来源必须讲清：价格由系统自动从 Steam 全球 40 多个区获取；
     添加时取一次，关注后每 6 小时自动更新一轮（与调度器价格轮周期一致）；
   · 添加游戏 ≠ 关注游戏：添加只拿一次价格，关注才持续更新——两条必须分开说；
   · Steam 账号 = 可选增强（同步愿望单 / 已购 / 家庭库），单独一步、标题带「可选」；
   · 代理与任务页不进导览（系统层能力，用户无需学习）。
   引导底部的「上一步 / 下一步 / 完成」复用 common.prev / common.next
   / common.finish（与 HlStepper 同词条，非本模块自有文案）。
   ════════════════════════════════════════════════════════════════════ */

const productTour = {
  /* ── 开场卡（无聚光靶点）── */
  'productTour.intro.title': '欢迎来到 {app}',
  'productTour.intro.p1':
    '{app} 帮你持续关注游戏价格：同一款游戏在全球 40 多个区的价格放在一起看，哪里便宜一目了然。',
  'productTour.intro.p2':
    '用一分钟走一遍主线——找游戏、看价格、关注想盯的游戏；之后系统自动更新，价格到位了提醒你。',
  'productTour.intro.p3':
    '中途随时可以点「跳过导览」；以后想重看，到「关于」页点 {app} logo 就行。',

  /* ── 第 1 步 · 找游戏（聚光搜索框）── */
  'productTour.stepFind.title': '第 1 步 · 找游戏',
  'productTour.stepFind.p1':
    '在搜索框输入游戏名，或用旁边的筛选挑出想看的——每张卡片是一款游戏。',
  'productTour.stepFind.p2':
    '各区现价、折扣和历史最低价直接印在卡片上；点开卡片进详情，能看完整的价格走势。',
  'productTour.stepFind.emph': '先找到想看的游戏，再决定要不要关注。',

  /* ── 第 2 步 · 价格从哪来（数据来源 + 添加 ≠ 关注）── */
  'productTour.stepPrice.title': '第 2 步 · 价格从哪来',
  'productTour.stepPrice.p1':
    '卡片上的各区价格由系统自动从 Steam 全球 40 多个区获取，取回来放在同一张卡片上比较。',
  'productTour.stepPrice.p2':
    '想看的游戏还不在列表里？粘贴它的游戏链接添加进来，系统会自动取回各区价格——不需要绑定 Steam 账号。',
  'productTour.stepPrice.emph':
    '添加游戏 ≠ 关注：添加只取一次价格；关注后每 6 小时自动更新一轮。',

  /* ── 第 3 步 · 我的关注（持续更新）── */
  'productTour.stepFollow.title': '第 3 步 · 我的关注',
  'productTour.stepFollow.p1':
    '想让系统以后持续帮你盯哪款，就在这里关注它——在「找游戏」页点卡片上的星标，或在本页添加。',
  'productTour.stepFollow.p2':
    '关注列表里随时能看到每款的最新价格和更新时间，也可以随时取消关注。',
  'productTour.stepFollow.emph': '关注后，系统会在后台持续更新价格。',

  /* ── 第 4 步 · 绑定 Steam 账号（可选增强）── */
  'productTour.stepBind.title': '可选 · 绑定 Steam 账号',
  'productTour.stepBind.emph':
    '不绑定也完全不影响前面的功能——这只影响自动化程度。',
  'productTour.stepBind.p1':
    '绑定后，你的愿望单、已购游戏和家庭库会自动同步进来持续跟踪比价，还能看到钱包余额。',
  'productTour.stepBind.p2':
    '点「登录并自动获取」按提示登录即可；这一步随时可以跳过，以后想绑再来。',
  'productTour.stepBind.hint':
    '找不到自动获取按钮时，这张卡片里有手动引导。',

  /* ── 收尾卡（无聚光靶点）── */
  'productTour.done.title': '完成！接下来交给系统',
  'productTour.done.emph':
    '找游戏 → 看价格 → 关注 → 自动更新——剩下的系统自动运行。',
  'productTour.done.p1':
    '关注的游戏每 6 小时自动更新；想「到价就通知」，到「价格提醒」页设一个条件就行。',
  'productTour.done.p2': '其他功能不急着了解，用到了再去探索。',
  'productTour.done.p3':
    '想重看本导览：到「关于」页点 {app} logo，或到「设置」页「新手教程」分节。',

  /* ── 浮层自身的控件 ── */
  'productTour.action.skip': '跳过导览',
  /* 步骤进度（气泡左上角胶囊）。分隔符 `/` 是中性符号，整条进词条不拼接。 */
  'productTour.progress': '{current} / {total}',
} as const

export default productTour
