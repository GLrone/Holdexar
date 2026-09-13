/* logs 词条 —— 运行日志页（views/logs/Index.vue）。

   「已复制」直接复用 common.copied（与按钮态同一件事），故此处没有该条。
   行结构着色（`时间 [级别] logger: 消息`）是后端原样字符串，不迁。 */

const logs = {
  /* SSE 连接状态 */
  'logs.stream.connected': '实时流已连接',
  'logs.stream.connecting': '连接中…（断线自动重连）',

  /* 工具行计数（中英语序不同：中文量词在后，英文复数在后） */
  'logs.count.lines': '{n} 行',
  'logs.count.errors': '错误 {n}',
  'logs.count.warnings': '警告 {n}',

  /* 工具行按钮 */
  'logs.action.resume': '回到底部并跟随',
  'logs.action.following': '跟随中',
  'logs.action.copyAll': '复制全部 {n} 行',
  'logs.action.clear': '清屏',

  /* 日志框 */
  'logs.empty': '暂无日志输出——后台爬取/汇率刷新/调度器运行时将实时打印到这里',
  'logs.line.clickToCopy': '单击复制该行',

  /* 复制结果提示 */
  'logs.copy.lineCopied': '已复制该行',
  'logs.copy.failed': '复制失败',
  'logs.copy.failedHint': '复制失败——请在日志框内手动选择后 Ctrl+C',
} as const

export default logs
