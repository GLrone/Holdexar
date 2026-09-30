/* ════════════════════════════════════════════════════════════════════
   领航员（pilot）词条：领航台抽屉（HlPilotDrawer.vue）与设置页领航员卡。
   用户面专属名词：领航员（助手）· 领航台（提问面板）· 航报（事实摘要）。
   领航员设置卡虽在设置页，文案仍归本模块——领航员词汇单一来源。
   ════════════════════════════════════════════════════════════════════ */

const pilot = {
  'pilot.title': '领航员',
  'pilot.console': '领航台',
  'pilot.briefing': '航报',
  'pilot.ask.entry': '问领航员',
  'pilot.ask.placeholder': '问问领航员：这游戏现在值得入手吗？',
  'pilot.ask.button': '提问',
  'pilot.context.of': '当前对象',
  'pilot.loading': '正在整理数据…',
  'pilot.thinking': '思考过程',
  'pilot.thinking.live': '思考中…',
  'pilot.cached': '缓存',
  'pilot.error': '领航台暂时没能完成这次问答，稍后再试。',
  'pilot.guide.title': '可以这样操作',
  'pilot.guide.monitor': '关注游戏：打开游戏详情，点「关注」即可持续追踪价格。',
  'pilot.guide.alert': '价格提醒：在游戏详情的提醒区设置「低于某价提醒我」。',
  'pilot.guide.link': '去游戏库逛逛',
  'pilot.reason.llm_off': 'AI 解读还没配置：在「设置 → 领航员」填入服务地址与密钥后即可用。',
  'pilot.reason.cap_reached': '本月 AI 解读额度已用完，可在「设置 → 领航员」调整上限；以下是真实数据的事实摘要。',
  'pilot.reason.llm_failed': 'AI 解读暂时不可用，以下是真实数据的事实摘要。',
  'pilot.reason.no_data': '库里还没找到相关的价格数据。可以先在「游戏库」添加游戏，再来问。',
  'pilot.reason.need_target': '库里找到几款相近的游戏，是哪一款？说一下名字就帮你办。',
  'pilot.facts.price': '《{name}》国区现价 {price}{discount}，史低 {lowest}。近一年：最低 {ymin} ／ 最高 {ymax}（{count} 条价格观测）。',
  'pilot.facts.price_noyear': '《{name}》国区现价 {price}{discount}，史低 {lowest}。',
  'pilot.facts.discount': '（-{discount}%）',
  'pilot.facts.noLowest': '暂无史低记录',
  'pilot.facts.gamesTitle': '库内可能相关的游戏：',
  'pilot.action.monitor': '《{name}》已加入关注。',
  'pilot.action.alertPrice': '《{name}》已设置价格提醒：中国区 ≤ {price}。',
  'pilot.action.alertLow': '《{name}》已设置史低提醒。',
  'pilot.settings.title': '领航员',
  'pilot.settings.desc': '比价助手「领航员」的 AI 解读来源，填入任意 OpenAI 兼容服务即可；不配置时领航台仍显示事实摘要。',
  'pilot.settings.enabled': '启用 AI 解读',
  'pilot.settings.base_url': '服务地址（OpenAI 兼容，填到 /v1 为止）',
  'pilot.settings.model': '模型名',
  'pilot.settings.api_key': 'API Key',
  'pilot.settings.api_key_hint': '加密存储；留空表示不修改',
  'pilot.settings.monthly_cap': '每月 token 上限',
  'pilot.settings.usage': '本月已用 {tokens} tokens',
  'pilot.settings.save': '保存领航员设置',
  'pilot.settings.saved': '已保存',
} as const

export default pilot
