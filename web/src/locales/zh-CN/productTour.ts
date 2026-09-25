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
   不是文案：`pool.section.items` / `alerts.section.rules`。它们既不出现在
   本文件，也不该建词条——DOM 锚点与语言无关，建了反而多一个会漂移的来源。
   视图侧写的是同一串字符串。`data-tour` 值（lib-empty / card-price /
   gamelib-main）同属契约，不需要词条。

   文案口径（按用户主链教学，P-M4）：
   · 主链 = 找游戏 → 查看各区价格 → 关注 → 系统自动更新 → 价格变化 → 提醒；
     引导里点名的界面标签逐字取自 P-M3 后的导航词典（找游戏 / 我的关注 /
     价格提醒 / 游戏库 / 设置）；
   · 添加游戏 ≠ 关注游戏：添加只拿一次价格，关注才持续更新——两条必须分开说；
   · Steam 账号 = 可选增强（同步愿望单 / 已购 / 家庭库），不是任何功能的前置；
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

  /* ── 第 1 步 · 找游戏 ── */
  'productTour.stepFind.title': '第 1 步 · 找游戏',
  'productTour.stepFind.p1':
    '这里是「找游戏」：可以搜索、浏览和筛选，每张卡片是一款游戏。',
  'productTour.stepFind.p2':
    '各区现价、折扣和历史最低价直接印在卡片上；点开卡片进详情，能看完整的价格走势。',
  'productTour.stepFind.emph': '先找到想看的游戏，再决定要不要关注。',

  /* ── 第 2 步 · 价格自动更新（添加 ≠ 关注）── */
  'productTour.stepPrice.title': '第 2 步 · 价格自动更新',
  'productTour.stepPrice.p1':
    '想看的游戏还不在列表里？粘贴游戏链接把它添加进来，系统会自动获取它的各区价格——不需要绑定 Steam 账号。',
  'productTour.stepPrice.p2':
    '价格到手后，卡片上的各区价格会随每轮更新自动刷新，不用你盯着。',
  'productTour.stepPrice.emph':
    '添加游戏 ≠ 关注：添加只拿一次价格；要不要长期盯，下一步自己决定。',

  /* ── 第 3 步 · 我的关注（持续更新）── */
  'productTour.stepFollow.title': '第 3 步 · 我的关注',
  'productTour.stepFollow.p1':
    '想让系统以后持续帮你盯哪款，就在这里关注它——在「找游戏」页点卡片上的星标，或在本页添加。',
  'productTour.stepFollow.p2':
    '关注列表里随时能看到每款的最新价格和更新时间，也可以随时取消关注。',
  'productTour.stepFollow.emph': '关注后，系统会在后台持续更新价格。',

  /* ── 第 4 步 · 价格提醒（到价通知）── */
  'productTour.stepAlert.title': '第 4 步 · 价格提醒',
  'productTour.stepAlert.p1':
    '设一个条件，比如「价格 ≤ 目标」——填一个你愿意出手的价；也可以随时暂停或重新启用提醒。',
  'productTour.stepAlert.p2':
    '点「添加」就完成了。每轮价格更新都会自动核对，达标即通知你；触发记录里能回看每次提醒。',
  'productTour.stepAlert.emph':
    '我的关注是系统持续帮你看；价格提醒是满足条件时主动通知你——两者互不依赖。',

  /* ── 第 5 步 · 游戏库（平行能力）── */
  'productTour.stepGamelib.title': '第 5 步 · 游戏库',
  'productTour.stepGamelib.p1':
    '这里查看你已经拥有或关联的游戏——库分析、家庭库、游玩动态都在这一组页面里。',
  'productTour.stepGamelib.p2':
    '绑定 Steam 账号后，愿望单、已购游戏和家庭库会自动同步进来；不绑定也不影响前面的价格功能。',

  /* ── 收尾卡（无聚光靶点）── */
  'productTour.done.title': '完成！接下来交给系统',
  'productTour.done.emph':
    '找游戏 → 看价格 → 关注 → 自动更新 → 到价提醒——剩下的系统自动运行。',
  'productTour.done.p1':
    '想增强自动化，可以到「设置」页绑定 Steam 账号（同步愿望单、已购游戏、家庭库），完全可选。',
  'productTour.done.p2': '其他功能不急着了解，用到了再去探索。',
  'productTour.done.p3':
    '想重看本导览：到「关于」页点 {app} logo，或到「设置」页「新手教程」分节。',

  /* ── 浮层自身的控件 ── */
  'productTour.action.skip': '跳过导览',
  /* 步骤进度（气泡左上角胶囊）。分隔符 `/` 是中性符号，整条进词条不拼接。 */
  'productTour.progress': '{current} / {total}',
} as const

export default productTour
