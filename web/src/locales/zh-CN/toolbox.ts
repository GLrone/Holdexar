/* toolbox 词条 —— views/toolbox/Index.vue（账户消费账单 + CDK 批量激活）。

   两个分节锚点（`data-section="toolbox.section.bill" / "toolbox.section.cdk"`）
   直接复用分节标题词条：HlSectionRail 读到后 t() 显示，DOM 锚点与语言无关；
   标题与锚点一条定义两处消费，不存在「改了标题忘了锚点」的漂移面。

   **本页的冻结陷阱集中在 CDK 侧**：进度文案、批次号、未绑 Cookie 提示原先都是
   写进 ref 的成品中文（赋值全在异步回调里）——一律改成「state 只存词条 key 或
   数字，句子在模板里 t() 现取」。账单表格的类型列同理（行数据一次性加工）。

   复用：「全部」chip 走 common.all；「游戏购买 / 钱包充值 / 退款」在筛选 chip、
   表格类型列、消费占比三处是同一件事，共用 toolbox.bill.type.*。 */

const toolbox = {
  /* ── 分节锚点（data-section 值 = 模块标题）── */
  'toolbox.section.bill': '账户消费账单',
  'toolbox.section.cdk': 'CDK 批量激活',

  /* ── 多账号切换（模块头部的账号下拉）── */
  'toolbox.account.noNickname': '未同步昵称',
  /* 下拉项整条入词条：昵称缺省值（上一条）作为参数传入，语序由本条决定 */
  'toolbox.account.optionLabel': '{name} · 好友码 {code}',
  'toolbox.account.switched': '已切换当前账号，激活配额按新账号重计',

  /* ── 账户消费账单 ── */
  'toolbox.bill.subtitle':
    '购买历史按类型筛选 · 多币种按入账日汇率统一折算人民币 · 最新 5 条摘要，全量见「完整账单」',
  'toolbox.bill.loading': '账单档案加载中…',
  'toolbox.bill.loadFailed': '账单加载失败：{msg}',
  'toolbox.bill.empty.title': '暂无账单数据',
  'toolbox.bill.empty.hint': '绑定 Steam Cookie 后账单自动同步，完整明细在「完整账单」页查看',
  'toolbox.bill.empty.action': '前往完整账单',
  /* 多档案 chip：昵称与「折算后金额 元」是一句话，整条参数化（英文语序不同） */
  'toolbox.bill.importChip': '{nick}（{amount} 元）',

  /* 四格 mini-stats */
  'toolbox.bill.stats.net': '累计净消费（退款已扣）',
  'toolbox.bill.stats.txCount': '购买交易总数',
  /* 「N 笔」的量词，独立小字号 span；英文不需要量词（见 en 侧留空） */
  'toolbox.bill.stats.txUnit': '笔',
  'toolbox.bill.stats.month': '本月消费',
  'toolbox.bill.stats.refund': '退款合计',

  /* 交易类型：同一语义三处复用（筛选 chip / 表格类型列 / 消费占比） */
  'toolbox.bill.type.game': '游戏购买',
  'toolbox.bill.type.wallet': '钱包充值',
  'toolbox.bill.type.market': '市场交易',
  'toolbox.bill.type.gift': '礼物购买',
  /* chip 与占比行用短名（无「购买」二字），与上一条不同语义故单列 */
  'toolbox.bill.type.giftShort': '礼物',
  'toolbox.bill.type.refund': '退款',

  /* 摘要表格 */
  'toolbox.bill.table.date': '日期',
  'toolbox.bill.table.item': '项目',
  'toolbox.bill.table.type': '类型',
  'toolbox.bill.table.amount': '金额',
  'toolbox.bill.table.noRows': '该分类暂无交易',
  'toolbox.bill.viewAll': '查看全部',

  /* 近 8 月趋势柱 + 消费类型占比 */
  'toolbox.bill.trend.title': '近 8 月消费趋势（人民币）',
  /* 柱下标：「3月」。英文侧不需要「月」字（标题已说明是月度视图） */
  'toolbox.bill.trend.monthLabel': '{month}月',
  'toolbox.bill.trend.empty': '暂无月度数据',
  'toolbox.bill.ratio.title': '消费类型占比',

  /* ── CDK 批量激活 ── */
  'toolbox.cdk.subtitle':
    '真实调用 Steam 激活端点（登录态 Cookie）· 每批 9 个串行 · 批间等待 20s · 结果含 SubID + 版本名',

  /* 未绑 Cookie 引导块 */
  'toolbox.cdk.needCookie.title': '激活需要 Steam 登录态 Cookie',
  'toolbox.cdk.needCookie.hint':
    '在「我」页绑定 Cookie（或用「登录并自动获取」）后，此处直接批量激活',
  'toolbox.cdk.needCookie.action': '前往「我」页绑定',
  /* 模块头部状态胶囊（账号下拉缺席时）与激活前的进度兜底文案，同一句 */
  'toolbox.cdk.quotaMissing': '尚未绑定 Steam Cookie，激活前请先在「我」页绑定（激活需要登录态）',
  'toolbox.cdk.bindFirst': '请先在「我」页绑定 Steam Cookie 再激活',

  /* 左栏：输入与进度 */
  'toolbox.cdk.input.title': '激活码输入（每行一个）',
  'toolbox.cdk.input.placeholder':
    '支持批量激活，可直接粘贴整段网页文字，自动识别 XXXXX-XXXXX-XXXXX 格式激活码',
  'toolbox.cdk.start': '开始激活',
  'toolbox.cdk.reset': '重置',
  'toolbox.cdk.batch.label': '批次 {current} / {total}',

  /* 进度文案 —— 存的是本组 key，不是成品译文（见文件头） */
  'toolbox.cdk.progress.ready': '就绪 · 待开始',
  'toolbox.cdk.progress.running': '激活中',
  'toolbox.cdk.progress.batchWait': '批间等待 20s（Steam 激活尝试限制节奏）',
  'toolbox.cdk.progress.done': '激活完成',
  'toolbox.cdk.progress.limitReached':
    '本账号激活次数已达上限（30分钟内10次）——换绑其他账号即可重计，或稍后再试',
  'toolbox.cdk.progress.noCookie': '未绑定 Steam Cookie',

  /* 配额说明条与账号计数 */
  'toolbox.cdk.limit.hint':
    '激活上限按账号独立：30 分钟内 10 次（无论成败）· 换绑账号即重计 · 单批最多 9 个',
  'toolbox.cdk.limit.used': '本账号已用 {used} / {total}',

  /* 激活队列 */
  'toolbox.cdk.queue.title': '激活队列',
  'toolbox.cdk.queue.empty': '左侧输入激活码后，队列将显示在此',

  /* 状态词。队列徽章与结果表徽章语义相同但用词有别（「激活成功」/「成功」），
     各自成条；summary.（无效/失败、已拥有/重复）是统计格标签，与徽章不同条 */
  'toolbox.cdk.status.wait': '等待中',
  'toolbox.cdk.status.doing': '激活中',
  'toolbox.cdk.status.queueOk': '激活成功',
  'toolbox.cdk.status.ok': '成功',
  'toolbox.cdk.status.own': '已拥有',
  'toolbox.cdk.status.fail': '失败',
  'toolbox.cdk.summary.fail': '无效/失败',
  'toolbox.cdk.summary.own': '已拥有/重复',

  /* 右栏：结果表 */
  'toolbox.cdk.result.title': '激活结果',
  'toolbox.cdk.result.copyTitle': '复制所有 SubID + 版本名（一行一个）',
  'toolbox.cdk.result.col.index': '序号',
  'toolbox.cdk.result.col.result': '结果',
  'toolbox.cdk.result.col.detail': '详情',
  'toolbox.cdk.result.col.game': '游戏 / 版本名',
  'toolbox.cdk.raw.expand': '展开 Steam 返回原文',
  'toolbox.cdk.raw.collapse': '收起 Steam 返回原文',
  'toolbox.cdk.failNoResult': '激活失败（响应无结果）',

  /* 完成汇总气泡：三档 × 「有无因配额跳过」。
     原实现是「主句 + 拼接后缀」，中英语序不同拼不出来，故每档各成一条参数化词条。 */
  'toolbox.cdk.done.ok': '激活完成：成功 {ok} · 已有 {own}',
  'toolbox.cdk.done.okSkipped': '激活完成：成功 {ok} · 已有 {own} · 因配额跳过 {skipped}',
  'toolbox.cdk.done.partial': '激活完成：成功 {ok} · 已有 {own} · 失败 {fail}',
  'toolbox.cdk.done.partialSkipped':
    '激活完成：成功 {ok} · 已有 {own} · 失败 {fail} · 跳过 {skipped}',
  'toolbox.cdk.done.allFailed': '激活全部失败（{fail} 个）——详情见结果列表',
  'toolbox.cdk.done.allFailedSkipped':
    '激活全部失败（{fail} 个）——详情见结果列表 · 配额跳过 {skipped}',
} as const

export default toolbox
