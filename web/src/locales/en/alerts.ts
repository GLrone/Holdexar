/* English 词典 · alerts（与 zh-CN/alerts.ts 同构，key 必须逐一对齐）。
   对应源文件：views/alerts/Index.vue。

   分节条目（`alerts.section.*`）既是 data-section 锚点也是区块标题；
   `alerts.section.rules` 被 ProductTour 的选择器引用，勿改名。

   `{host}` / `{email}` 是视图传进来的**示例值**（词典内不放任何地址字面量），
   `{masked}` 是服务端返回的掩码串。 */

import type { MessageKey } from '../zh-CN'

const alerts: Partial<Record<MessageKey, string>> = {
  /* Section anchors, doubling as card titles */
  'alerts.section.rules': 'Add alert rule',
  'alerts.section.list': 'Alert rules',
  'alerts.section.history': 'Trigger history',
  'alerts.section.smtp': 'Email notifications',

  /* Add rule: search */
  'alerts.rules.desc':
    'Search by game name (Chinese or English) or AppID. Local database matches come first; AppIDs with no local record are verified online.',
  'alerts.rules.searchPlaceholder': 'Search by game name (Chinese or English) or AppID',
  'alerts.rules.search': 'Search',
  'alerts.rules.clear': 'Clear',
  'alerts.rules.add': 'Add',

  /* Target-type picker */
  'alerts.rules.optionPrice': 'Price ≤ target',
  'alerts.rules.optionPct': 'Discount ≥ target',
  'alerts.rules.optionHistoricLow': 'Below historic low',

  /* Target-value placeholder / read-only hint */
  'alerts.rules.hintHistoric': 'Compared automatically against the historic low',
  'alerts.rules.hintPrice': 'CNY, e.g. 50 = ¥50',
  'alerts.rules.hintPct': '%, e.g. 50 = 50% off or more',

  /* Rule list */
  'alerts.rules.empty': 'No rules yet',
  'alerts.rules.never': 'Never',
  'alerts.rules.editTitle': 'Edit rule #{id}',
  'alerts.rules.targetValuePlaceholder': 'Target',
  'alerts.rules.save': 'Save',
  'alerts.rules.removeConfirm': 'Delete rule #{id} ({name})?',
  'alerts.rules.confirmTitle': 'Confirm',

  /* Condition column (label and value form one sentence) */
  'alerts.rules.condPrice': 'Price ≤ {value}',
  'alerts.rules.condPct': 'Discount ≥ {value}%',
  'alerts.rules.condHistoricLow': 'New low',

  /* Shared table headers */
  'alerts.table.game': 'Game',
  'alerts.table.region': 'Region',
  'alerts.table.condition': 'Condition',
  'alerts.table.enabled': 'Enabled',
  'alerts.table.lastTriggered': 'Last triggered',
  'alerts.table.actions': 'Actions',
  'alerts.table.time': 'Time',
  'alerts.table.priceThen': 'Price then',
  'alerts.table.email': 'Email',

  /* Trigger history */
  'alerts.history.empty': 'No triggers yet',
  'alerts.history.notifiedEmail': 'Sent',
  'alerts.history.notifiedInApp': 'In-app',

  /* Email notifications (SMTP labels and hints only) */
  'alerts.smtp.desc':
    'Once SMTP is set up, a price alert sends an email automatically. The password is stored only in the local database.',
  'alerts.smtp.host': 'SMTP server',
  'alerts.smtp.hostPlaceholder': 'e.g. {host}',
  'alerts.smtp.port': 'Port',
  'alerts.smtp.user': 'From address',
  'alerts.smtp.userPlaceholder': 'e.g. {email}',
  'alerts.smtp.password': 'Auth code / password',
  'alerts.smtp.passwordPlaceholder': 'Enter the auth code or password',
  'alerts.smtp.passwordConfigured': 'Configured ({masked}) — leave blank to keep',
  'alerts.smtp.toAddr': 'To address',
  'alerts.smtp.toAddrPlaceholder': 'Address that receives the alerts',
  'alerts.smtp.encryption': 'Encryption',
  'alerts.smtp.ssl': 'SSL (465)',
  'alerts.smtp.starttls': 'STARTTLS (587)',
  'alerts.smtp.test': 'Test connection',
  'alerts.smtp.save': 'Save email settings',

  /* Action feedback */
  'alerts.toast.selectGameFirst': 'Search for and select a game first',
  'alerts.toast.enterTarget': 'Enter a target value',
  'alerts.toast.added': 'Rule added — checked automatically after the next crawl',
  'alerts.toast.enabled': 'Rule enabled',
  'alerts.toast.disabled': 'Rule disabled',
  'alerts.toast.updated': 'Rule updated',
  'alerts.toast.removed': 'Rule #{id} deleted',
  'alerts.toast.smtpSaved': 'Email settings saved',
  'alerts.toast.testSent': 'Test email sent to {to} — check your inbox',
}

export default alerts
