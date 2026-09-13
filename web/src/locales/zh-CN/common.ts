/* ════════════════════════════════════════════════════════════════════
   通用词条：跨模块复用的短文案（组件默认值、状态词、按钮）。
   判据是「与业务无关、多个模块都要用」——只在一个视图出现的文案，
   请放进那个视图的模块文件，别往这里堆。
   ════════════════════════════════════════════════════════════════════ */

const common = {
  'common.empty': '暂无数据',
  'common.loading': '加载中…',
  'common.confirm': '确定',
  'common.cancel': '取消',
  'common.close': '关闭',
  'common.retry': '重试',
  'common.refresh': '刷新',
  'common.copy': '复制',
  'common.copied': '已复制',
  'common.all': '全部',
  'common.total': '合计',

  /* 通用动作（监控地区等分节的按键）：与业务无关、多处可复用，故在 common。 */
  'common.save': '保存',
  'common.selectAll': '全选',
  'common.clear': '清空',

  /* 控件默认值（HlSelect / HlDatePicker / HlPopconfirm）——
     注意这些**不能**写进 withDefaults：那里的默认值只在 defineProps
     求值那一刻算一次，会把语言冻结在组件创建时。 */
  'common.select': '请选择',
  'common.selectDate': '选择日期',
  'common.confirmTip': '确定执行该操作？',

  /* 步进器（HlStepper）页脚与完成页 */
  'common.prev': '上一步',
  'common.next': '下一步',
  'common.finish': '完成',
  'common.restart': '重新开始',
  'stepper.doneTitle': '全部就绪 🎉',
  'stepper.doneHint': '引导已完成，可随时重新开始。',

  /* 时长单位 —— 跨模块共用，故在 common 而不在任何一个 family 模块里。
     「h」不是语言中立的：中文写 `128h`，英文写 `128 hrs`。带 k 的简写同理
     （KPI 卡里数字要短）。原先两个 tab 各自手写这两条，其中一处还把空格写成
     `'k h'`，与另一处不一致——放到这里就是为了让它们只能有一份。 */
  'common.hours': '{h}h',
  'common.hoursK': '{h}kh',

  /* 窄列区名简写的后缀（api/regions.ts 的 compactRegionName 第二个参数）。
     「哈萨克斯坦」→「哈萨区」是中文的排版习惯（取前两字 + 区），英文照搬会得到
     「Ka区」——所以后缀必须随语言给：中文 `区`，英文**空串**。
     空串在这个出口里是**有意义的取值**，不是缺译：它表示"不做简写，原样返回"，
     英文的截断交给 CSS 的 ellipsis（"Kazakhstan" 截成 "Kazakh…" 远好过 "Ka区"）。
     调用点见 components/RegionFlag.vue 与 steamhl/HlGameCard.vue 的列表布局。 */
  'common.regionSuffix': '区',

  /* 页内分节轨（HlSectionRail）：分节无 data-section 名时的兜底标签 */
  'rail.unnamed': '分节',
  'rail.navLabel': '页面分节导航',
  'rail.jumpTo': '定位到「{label}」',

  /* 「施工中」占位视图（views/_stub/Missing.vue）——构建期兜底，
     正常不应出现在用户面前，但一旦出现它就是用户可见文案。 */
  'stub.title': '模块施工中',
  'stub.hint': '路由已挂载但视图尚未完成（并行施工半成品）。正式页面落地后此占位自动消失。',
} as const

export default common
