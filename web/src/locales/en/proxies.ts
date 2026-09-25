/* English 词典 · proxies（与 zh-CN/proxies.ts 同构，key 必须逐一对齐）。
   对应源文件：views/proxies/Index.vue。

   两个契约值：`proxies.section.clash` 的中文值必须是「Clash 接入」——它的
   **key** 被 ProductTour 的 target 逐字引用，两个文件同时改才不断引导。

   表格列头与徽标是固定窄列（70–130px），一律取最短的英语说法：
   「通/不通」= Yes/No，「同出口」= Same exit，「耗时」= Duration。 */

import type { MessageKey } from '../zh-CN'

const proxies: Partial<Record<MessageKey, string>> = {
  /* Section anchors + block titles (data-section holds these keys) */
  'proxies.section.routing': 'Routing policy',
  'proxies.section.clash': 'Clash setup',
  'proxies.section.nodes': 'Proxy nodes',
  'proxies.section.console': 'Routing console',

  /* Hero */
  'proxies.hero.title': 'Proxy IP pool',
  'proxies.hero.subtitle': 'Manage egress proxies — routing policy, subscriptions, reachability checks and routing logs',

  /* Routing-policy cards (desc carries <br><small>, rendered via v-html) */
  'proxies.strategy.proxyFirst.label': 'Proxy first (recommended)',
  'proxies.strategy.proxyFirst.desc': 'Clash when running<br><small>otherwise rotate the pool, then go direct</small>',
  'proxies.strategy.directOnly.label': 'Direct only',
  'proxies.strategy.directOnly.desc': 'All traffic on the local network<br><small>no proxy at all</small>',
  'proxies.strategy.directFirst.label': 'Direct first',
  'proxies.strategy.directFirst.desc': 'Local first<br><small>retry through a proxy on failure</small>',
  'proxies.strategy.proxyOnly.label': 'Proxy only',
  'proxies.strategy.proxyOnly.desc': 'Every request through the pool<br><small>rotated per request</small>',
  'proxies.strategy.on': 'On',
  'proxies.strategy.off': 'Off',
  'proxies.strategy.updated': 'Routing policy updated (takes effect on the next job)',

  /* Local mixed port (standalone config, unaffected by policy) */
  'proxies.port.label': 'Local mixed port',
  'proxies.port.title': 'Mixed port of a Clash/Verge you start yourself (proxy-first fallback probes it)',
  'proxies.port.invalid': 'Port must be an integer between 1024 and 65535',
  'proxies.port.updated': 'Clash port updated',

  /* Kernel download / install */
  'proxies.kernel.dialogTitle': 'Downloading kernel',
  'proxies.kernel.phasePrepare': 'Preparing download',
  'proxies.kernel.sizeHint': '· mihomo, about 15 MB',
  'proxies.kernel.viaWrap': '({via})',
  'proxies.kernel.downloading': 'Downloading…',
  'proxies.kernel.autoDownload': 'Install kernel',
  'proxies.kernel.downloadDone': 'Kernel downloaded',
  'proxies.kernel.downloadFailed': 'Kernel download failed: {error}',
  'proxies.kernel.orPlaceManual': 'or place it manually in {dir}',
  'proxies.kernel.pathHint': 'Kernel path {path}',
  'proxies.kernel.installOk': 'Kernel installed: {version}',
  'proxies.kernel.installFailed': 'Installation failed',
  'proxies.kernel.downloadHint': 'Downloading and installing the Clash kernel — the subscription is saved and its nodes fetched afterwards…',
  'proxies.kernel.ready': 'Kernel ready',
  'proxies.kernel.missing': 'Kernel missing',

  /* Clash run state and start/stop */
  'proxies.clash.running': 'Running · port {port}',
  'proxies.clash.stopped': 'Not running',
  'proxies.clash.notRunningTitle': 'Clash is not running',
  'proxies.clash.start': 'Start',
  'proxies.clash.stop': 'Stop',
  'proxies.clash.started': 'Clash started on mixed port {port}, checking nodes in the background…',
  'proxies.clash.hasStopped': 'Clash stopped',
  'proxies.clash.startHint': 'Starting pulls the selected subscription and launches the kernel, then checks nodes in the background (queued behind manual checks)',
  'proxies.clash.needSub': 'Add a Clash subscription link first (kept locally for the long term)',
  'proxies.clash.allDeprecated': 'Every Clash subscription is deprecated (over 95% of nodes unusable) — delete or replace one manually',
  'proxies.clash.switchedToUsable': 'The selected subscription is deprecated — switched to the most recent usable one',

  /* Clash node check */
  'proxies.clash.testNodes': 'Check nodes (Steam reachability)',
  'proxies.clash.testingProgress': 'Checking {done}/{total}',
  'proxies.clash.queued': 'Waiting for the current check…',
  'proxies.clash.testFailed': 'Check failed',
  'proxies.clash.collapse': 'Hide',
  'proxies.clash.expand': 'Show',
  'proxies.clash.cooldownSkipped': '{n} in cooldown skipped',
  'proxies.clash.testDone': 'Check complete: {alive}/{total} reach Steam',
  'proxies.clash.testDoneDeprecated': '{msg} — over 95% of its nodes are unusable, so it was marked deprecated (the backend will stop using it; delete or replace it manually)',
  'proxies.clash.aliveTag': 'Steam OK {alive}/{total}',
  'proxies.clash.uniqueExits': '{n} unique exits',
  'proxies.clash.reachable': 'Yes',
  'proxies.clash.unreachable': 'No',
  'proxies.clash.healthy': 'Healthy',
  'proxies.clash.unavailable': 'Unavailable',
  'proxies.clash.cooling': 'Cooling down',
  'proxies.clash.coolingTitle': 'Skipped while in cooldown — the previous verdict stands',
  'proxies.clash.sameExit': 'Same exit',

  /* Subscriptions (shared by the Clash and plain-text lanes) */
  'proxies.sub.save': 'Save subscription',
  'proxies.sub.edit': 'Edit',
  'proxies.sub.editTitle': 'Edit subscription',
  'proxies.sub.nameLabel': 'Subscription name',
  'proxies.sub.namePlaceholder': 'Leave empty to auto-detect on fetch (the result is stored locally)',
  'proxies.sub.urlLabel': 'Subscription URL',
  'proxies.sub.urlPlaceholder': 'https://… subscription URL',
  'proxies.sub.editSave': 'Save',
  'proxies.sub.autoLabel': 'Auto-update subscription',
  'proxies.sub.autoHint': 'When off, this subscription is no longer re-fetched on schedule (manual re-fetch still works); recommended for time-limited subscriptions',
  'proxies.sub.editTip': 'Saving a new URL re-fetches it automatically (direct first; if that fails, a saved working proxy is used)',
  'proxies.sub.edited': 'Subscription updated',
  'proxies.sub.editedSynced': 'Subscription updated and re-fetched ({parts})',
  'proxies.sub.urlInvalid': 'The subscription URL must be an http(s) link',
  'proxies.sub.savedNodes': 'Subscription saved ({nodes} nodes)',
  'proxies.sub.saved': 'Subscription saved',
  'proxies.sub.savedPlain': 'Subscription saved (kept locally for the long term)',
  'proxies.sub.kernelInstalled': 'Kernel installed automatically',
  'proxies.sub.kernelInstalledVersion': 'Kernel {version} installed automatically',
  'proxies.sub.deleted': 'Subscription deleted ({name})',
  'proxies.sub.confirmDelete': 'Delete this subscription link?',
  'proxies.sub.confirmDeleteDeprecated': 'This subscription is deprecated ({reason}) and no longer used by the backend. Delete it?',
  'proxies.sub.deleteDeprecatedTitle': 'Delete deprecated subscription',
  'proxies.sub.deprecatedReason': 'over 95% of its nodes are unusable',
  'proxies.sub.deprecated': 'Deprecated',
  'proxies.sub.aliveTag': '{alive}/{total} alive',
  'proxies.sub.syncing': 'Fetching…',
  'proxies.sub.refetch': 'Re-fetch',
  'proxies.sub.synced': 'Re-fetched ({parts})',
  'proxies.sub.syncedRestarted': 'Re-fetched ({parts}) — kernel restarted, checking nodes in the background…',
  'proxies.sub.nodesCount': '{n} nodes',
  'proxies.sub.trafficUsed': 'Used {size} GB',
  'proxies.sub.emptyClash': 'No Clash subscription yet — add one below; it is kept locally and pulled on start',
  'proxies.sub.allDeprecated': 'All subscriptions are deprecated (over 95% of nodes unusable) — the backend has stopped using them; delete or replace them manually',
  'proxies.sub.clashPlaceholder': 'Add a Clash subscription link (https://… airport subscription, stored locally only)',
  'proxies.sub.failTitle': 'Could not save subscription',
  'proxies.sub.failTip': 'Turn on a proxy first (e.g. Clash Verge on your desktop, or this app\'s Clash kernel), then click Retry to save the subscription again',

  /* Plain-text proxy subscriptions */
  'proxies.plain.empty': 'No proxy subscription yet — add a plain-text subscription link (commercial crawler proxy protocol, not Clash) to import a node pool in one click',
  'proxies.plain.placeholder': 'Add a proxy subscription link (https://… plain host:port / scheme://user:pass@host:port)',
  'proxies.plain.import': 'Import nodes',
  'proxies.plain.importing': 'Importing…',
  'proxies.plain.importToast': 'Fetching the subscription and importing nodes…',
  'proxies.plain.imported': 'Import complete: {added} added, {skipped} duplicates skipped',
  'proxies.plain.importedChecked': 'Import complete: {added} added, {skipped} duplicates skipped, {alive}/{checked} verified working',
  'proxies.plain.lastImport': 'Last import +{n}',

  /* Node pool */
  'proxies.node.summary': '{total} total · {enabled} enabled',
  'proxies.node.testAll': 'Test all',
  'proxies.node.testingAll': 'Testing…',
  'proxies.node.checking': 'Checking…',
  'proxies.node.testAllDone': 'Pool test complete',
  'proxies.node.clear': 'Clear',
  'proxies.node.cleared': 'Cleared',
  'proxies.node.confirmClear': 'Remove all {n} proxies?',
  'proxies.node.add': 'Add',
  'proxies.node.added': 'Added',
  'proxies.node.addedCount': 'Added {n} proxies',
  'proxies.node.manualPlaceholder': 'Add manually: host:port or scheme://user:pass@host:port (Enter to confirm)',
  'proxies.node.batchImport': 'Bulk import',
  'proxies.node.batchPlaceholder': 'Bulk import: one per line (host:port / host:port:user:pass / scheme://user:pass@host:port)',
  'proxies.node.empty': 'No proxies yet — save a subscription and import it in one click, or add one manually',
  'proxies.node.enabled': 'Proxy enabled',
  'proxies.node.disabled': 'Proxy disabled',
  'proxies.node.deleted': 'Proxy deleted',
  'proxies.node.confirmDelete': 'Delete proxy {url}?',
  'proxies.node.test': 'Test',
  'proxies.node.testOk': 'Working, {ms}ms latency',
  'proxies.node.testFailed': 'Unavailable: {error}',
  'proxies.node.unknownError': 'unknown error',
  'proxies.node.colAddress': 'Node address',
  'proxies.node.colExitIp': 'Exit IP',
  'proxies.node.colStatus': 'Status',
  'proxies.node.colDuration': 'Duration',
  'proxies.node.colLatency': 'Latency',
  'proxies.node.colEnabled': 'Enabled',
  'proxies.node.colActions': 'Actions',

  /* Routing console */
  'proxies.console.count': '{n} entries',
  'proxies.console.empty': 'No routing records yet',

  /* Actions and dialogs (module-local; nothing shares a value with common) */
  'proxies.action.delete': 'Delete',
  'proxies.dialog.confirm': 'Confirm',
}

export default proxies
