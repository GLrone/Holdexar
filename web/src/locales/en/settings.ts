/* English 词典 · settings（与 zh-CN/settings.ts 同构，key 必须逐一对齐）。

   `settings.section.steamAccount` 的值**不是自由文案**：ProductTour 的
   `target: '[data-section="settings.section.steamAccount"]'` 与视图侧
   data-section 用的是 key 串本身，与这里显示成什么无关——但改 key 会同时
   掐断两处，别动。

   `update.networkPost` / `update.noReleasePost` **带前导空格**（拼在
   「releases page」链接节点之后，模板空白节点在 `</a>` 与 `{{ }}` 之间
   不保留），同 zh 侧说明，不是笔误。 */

import type { MessageKey } from '../zh-CN'

const settings: Partial<Record<MessageKey, string>> = {
  /* Section anchors (data-section) + card titles */
  'settings.section.steamAccount': 'Steam account',
  'settings.section.account': 'Account',
  'settings.section.backup': 'Backups',
  'settings.section.update': 'App updates',
  'settings.section.tour': 'Guided tour',

  /* Steam account binding card */
  'settings.steam.desc': 'Binding an account shows its wallet balance (top right) plus billing currency and region. Only the sign-in Cookie fields are kept — including steamRefresh_steam, which renews the session automatically — stored in plain text in the local database only.',
  'settings.steam.cookiePlaceholder': 'Paste a Cookie containing steamLoginSecure (one long line or several lines — it is tidied up for you)',
  'settings.steam.cookiePlaceholderBound': 'The current account is bound ({identity}) — paste another to add it',
  'settings.steam.autoFetch': 'Sign in and fetch',
  'settings.steam.bind': 'Bind',
  'settings.steam.addAccount': 'Add account',
  'settings.steam.refreshBalance': 'Refresh balance',

  /* Cookie guide (steps 2-4 are whole sentences; inline tags live in the value) */
  'settings.steam.guideSummary': 'How do I get my Cookie? (Use “Sign in and fetch” above, or expand this for manual steps)',
  'settings.steam.guideStep1Pre': 'In a browser you use (Edge / Chrome), open',
  'settings.steam.guideStep1Post': 'and sign in to your Steam account.',
  'settings.steam.guideStep2': 'Press <kbd>F12</kbd> to open DevTools, switch to the <b>Network</b> tab, then press <kbd>F5</kbd> to reload the page.',
  'settings.steam.guideStep3': 'Click the <b>first request</b> in the list (usually the store page itself), find the one line that starts with <code>Cookie:</code> under Request Headers on the right, then <b>right-click → Copy value</b> (it is long — copy the whole line).',
  'settings.steam.guideStep4': 'Come back here, paste it into the field above and click “Bind”. Both the single line carrying the <code>Cookie:</code> prefix and the multi-line form copied row by row from Application → Cookies are recognized and tidied up automatically.',
  'settings.steam.guideNotes': 'The sign-in cookies kept are sessionid, steamCountry, steamLoginSecure and steamRefresh_steam (the renewal credential, present only if you ticked “Remember me” when signing in); anything else is dropped on save. The Cookie is stored in plain text in the local database only. With the renewal credential the session renews itself, so it only breaks once you sign out of Steam.',

  /* Bound-account list */
  'settings.steam.mismatchWarn': 'This account has a different SteamID from the SteamID64 saved above — check that it is the same account',
  'settings.steam.sessionExpiredWarn': 'Steam sign-in has expired and there is no renewal credential (you did not tick "Remember me" when signing in): click "Sign in and fetch" above to sign in again and tick it, or re-paste the Cookie using the guide below.',
  'settings.steam.sessionRenewingWarn': 'The Steam session is renewing itself: this usually recovers within minutes and needs no action; if it stays like this, click "Sign in and fetch" above to sign in again.',
  'settings.steam.syncExpired': 'Sign-in expired',
  'settings.steam.syncRenewing': 'Renewing',
  'settings.steam.syncTipExpired': 'Steam sign-in has expired - sign in again from the settings page ("Sign in and fetch")',
  'settings.steam.syncTipRenewing': 'Steam sign-in has expired and is renewing automatically (retries on failure)',
  'settings.steam.syncOk': 'Synced',
  'settings.steam.syncFail': 'Sync failed',
  'settings.steam.syncIdle': 'Not synced',
  'settings.steam.syncTipOk': 'Last synced: {time}',
  'settings.steam.syncTipFail': 'Last sync failed at {time} · {error}',
  'settings.steam.syncTipIdle': 'No wallet data synced yet',
  'settings.steam.noNickname': '(nickname not synced)',
  'settings.steam.primary': 'Primary',
  'settings.steam.active': 'Active',
  'settings.steam.friendCode': 'Friend code {code}',
  'settings.steam.friendCodeUnknown': 'Friend code unknown',
  'settings.steam.unknown': 'unknown',
  'settings.steam.balance': 'Balance',
  'settings.steam.currency': 'Currency',
  'settings.steam.region': 'Region',
  'settings.steam.games': 'Games',
  'settings.steam.wishlist': 'Wishlist',
  'settings.steam.redeems': 'Redeems',
  'settings.steam.setActive': 'Set active',
  'settings.steam.unbind': 'Unbind',
  'settings.steam.syncMeta': 'Last synced {time} · the wallet refreshes every minute (accounts staggered at random)',

  /* Account card (SteamID64 / Web API Key) */
  'settings.account.desc': 'Steam identity used for watch-pool sync (wishlist / owned games).',
  'settings.account.steamIdPlaceholder': 'e.g. 76561198000000000',
  'settings.account.lookup': 'Look up',
  'settings.account.applyFree': 'Get one free',
  'settings.account.apiKeyHint': 'Required for owned-games sync · stored in plain text in the local database only',
  'settings.account.apiKeyPlaceholder': 'Enter your API Key',
  'settings.account.apiKeyPlaceholderSet': 'Configured ({mask}) — leave blank to keep it',
  'settings.account.save': 'Save',

  /* Backup card */
  'settings.backup.desc': 'Online snapshot backups: no lock on the database file, no interruption to crawler writes, and every committed row included. One automatic backup per day, keeping the 5 most recent. Restoring is destructive — it replaces the whole database with the backup file, so verify first.',
  'settings.backup.createNow': 'Back up now',
  'settings.backup.snapshotting': 'Snapshotting…',
  'settings.backup.count': '{n} backups',
  'settings.backup.verify': 'Verify',
  'settings.backup.verifying': 'Verifying…',
  'settings.backup.download': 'Download',
  'settings.backup.restore': 'Restore',
  'settings.backup.restoring': 'Restoring…',
  'settings.backup.confirmRestore': 'Confirm restore?',
  'settings.backup.remove': 'Delete',
  'settings.backup.removing': 'Deleting…',
  'settings.backup.empty': 'No backups yet — click “Back up now” to create the first one.',

  /* App update card */
  'settings.update.desc': 'Reads the release manifest to compare versions, then downloads and verifies the SHA256 — restart the app to finish. Updates replace program files only; your data (games, prices, accounts, backups) lives in its own data directory and is never touched.',
  'settings.update.check': 'Check for updates',
  'settings.update.checking': 'Checking…',
  'settings.update.currentVersion': 'Current version v{version}',
  'settings.update.pendingReady': 'v{version} is downloaded and verified — restart the app to finish updating.',
  'settings.update.restartNow': 'Restart and update',
  'settings.update.later': 'Not now',
  'settings.update.download': 'Download v{version}',
  'settings.update.downloading': 'Downloading…',
  'settings.update.noChecksum': '(this release has no checksum)',
  'settings.update.networkPre': 'GitHub is unreachable right now. Try again later, or open the',
  'settings.update.noReleasePre': 'No published release found yet. Try again later, or check the',
  'settings.update.releasesPage': 'releases page',
  'settings.update.networkPost': ' to download manually.',
  'settings.update.noReleasePost': ' for updates.',
  'settings.update.upToDate': 'You are on the latest version (v{version}).',
  'settings.update.availableHint':
    'v{version} is available — use "Check for updates" to download and install it from the update dialog.',
  'settings.update.phaseDownloading': 'Downloading {progress}',
  'settings.update.phaseVerifying': 'Verifying SHA256…',
  'settings.update.phaseExtracting': 'Extracting…',
  'settings.update.phaseProcessing': 'Working…',
  /* Update behaviour switches (alerts / silent auto-update) */
  'settings.update.notifyLabel': 'New version alerts',
  'settings.update.notifyHint':
    'Pop up a dialog and light a dot on "Me" in the sidebar when an update is found. Turn it off to stay silent — you can still check manually any time.',
  'settings.update.notifyOn': 'New version alerts turned on',
  'settings.update.notifyOff': 'New version alerts turned off — no more interruptions',
  'settings.update.autoLabel': 'Silent auto-update',
  'settings.update.autoHint':
    'Download and verify new versions in the background with no popup; the update is applied the next time you start the app.',
  'settings.update.autoOn': 'Silent auto-update turned on — downloads will run in the background',
  'settings.update.autoOff': 'Silent auto-update turned off — downloads are manual again',
  'settings.update.autoStarted': 'Downloading v{version} in the background — we will not interrupt you',
  'settings.update.switchFailed': 'Could not save the setting — please try again',

  /* Guided-tour card (the tour copy itself lives in ProductTour.vue) */
  'settings.tour.desc': 'A spotlight walkthrough: add games → watch prices → set an alert — the three main steps, and the app is yours. Proxy and account binding are marked optional and can be skipped. It ran automatically on first launch — replay it any time.',
  'settings.tour.replay': 'Replay the tour',

  'settings.section.toolbox': 'Toolbox',
  'settings.toolbox.desc': 'Bill summaries, bulk CDK activation and other helper tools.',
  'settings.toolbox.open': 'Open Toolbox',

  /* Result toasts */
  'settings.toast.accountSwitched': 'Active account switched',
  'settings.toast.accountUnbound': 'Unbound {name}',
  'settings.toast.backupCreated': 'Backup created: {name} ({size}, {games} games, integrity verified)',
  'settings.toast.backupVerified': 'Verified: {name} ({games} games)',
  'settings.toast.backupVerifyFailed': 'Verification failed: {name} is not self-consistent — do not restore from it',
  'settings.toast.backupRestored': 'Restored from {name} — the data is now live',
  'settings.toast.backupRemoved': 'Deleted {name}',
  'settings.toast.updateCheckFailed': 'Update check failed: GitHub is unreachable right now (try again later, or download it manually)',
  'settings.toast.updateDownloaded': 'The new version is downloaded and verified — restart the app to finish updating',
  'settings.toast.updateDownloadFailed': 'Download failed: {error}',
  'settings.toast.updateCancelled': 'Update abandoned, staged files cleared',
  'settings.toast.restartFailed': 'Restart failed — please restart the app manually',
  'settings.toast.restartUnsupported': 'Automatic restart is desktop-only — please restart the app manually',
  'settings.toast.restartError': 'Restart call failed — please restart the app manually',
  'settings.toast.accountSaved': 'Account details saved',
  'settings.toast.cookieMismatch': 'The Cookie does not match the saved SteamID64',
  'settings.toast.bindSuccess': 'Bound — wallet balance {balance}',
  'settings.toast.bindSyncFailed': 'Cookie saved, but fetching the balance failed: {error}',
  'settings.toast.cookieSaved': 'Cookie saved',
  'settings.toast.noRefreshToken': 'Saved, but “Remember me” was not ticked when signing in: this session expires in about a day — tick it and sign in again.',
  'settings.toast.cookieEmpty': 'Paste a Cookie first',
  'settings.toast.desktopOnly': 'Auto-fetch works in the desktop window only — in a browser, follow the guide below and paste it manually',
  'settings.toast.loginOpened': 'Steam sign-in window opened — it returns here once you sign in…',
  'settings.toast.rebindDetected': 'Account {id} is already bound — rebinding…',
  'settings.toast.accountRecognized': 'Account {id} recognized',
  'settings.toast.cookieFetchFailed': 'No Cookie was returned',
  'settings.toast.fetching': 'Fetching…',
  'settings.toast.walletRefreshed': 'Refreshed',
  'settings.toast.walletRefreshFailed': 'Refresh failed',
  // Binding risk dialog (pops up on every binding action; 5s confirm countdown)
  // Items come in key pairs: Lead = bolded keyword, Rest = short explanation.
  'settings.risk.title': 'Read before binding a Steam account',
  'settings.risk.bodyTitle': 'Risk notice',
  'settings.risk.item1Lead': 'Credential stored in plain text',
  'settings.risk.item1Rest':
    'Your Steam login Cookie is saved unencrypted in the local database — as effective as handing the account to this app.',
  'settings.risk.item2Lead': 'Local use only',
  'settings.risk.item2Rest': 'Never uploaded to any server; only local features (wallet, wishlist, library sync).',
  'settings.risk.item3Lead': 'Readable from this machine',
  'settings.risk.item3Rest': 'Shared computers or malware may obtain it — assess your device environment.',
  'settings.risk.item4Lead': 'Prefer an alt account',
  'settings.risk.item4Rest': 'If the main account feels risky, bind a dedicated alternate Steam account.',
  'settings.risk.leakTitle': 'If the Cookie has leaked',
  'settings.risk.leak1Lead': 'Change your Steam password now',
  'settings.risk.leak1Rest':
    'All old sessions become invalid — the leaked Cookie is useless (this also signs out all sessions).',
  'settings.risk.leak2Lead': 'Enable Steam Guard',
  'settings.risk.leak2Rest': 'Make sure the mobile authenticator is on.',
  'settings.risk.leak3Lead': 'Review recent activity',
  'settings.risk.leak3Rest': 'Check sign-ins, trades and market history; contact Steam Support if anything looks wrong.',
  'settings.risk.leak4Lead': 'Unbind and clear it here',
  'settings.risk.leak4Rest': "Use 'Unbind all accounts' in settings to remove the stored credential.",
  'settings.risk.confirm': 'I understand, continue',
  'settings.risk.countdown': 'I understand ({n}s)',

  /* Notifications (price-event notices; categories are user-facing, no internal enums) */
  'settings.section.notification': 'Notifications',
  'settings.notification.desc':
    'Email yourself when a refresh produces price changes. Off by default — turning it on is your call.',
  'settings.notification.enabledLabel': 'Enable price notifications',
  'settings.notification.enabledHint':
    'When off, nothing is queued and nothing builds up; turning it back on will not resend older changes.',
  'settings.notification.categoriesLabel': 'Categories',
  'settings.notification.categoriesHint':
    'Event types are grouped into these four — switch one off to stop that kind of change.',
  'settings.notification.quietLabel': 'Quiet hours',
  'settings.notification.quietHint':
    'Changes produced during quiet hours are not sent immediately — they ride along with the next refresh (never dropped).',
  'settings.notification.detailsLabel': 'List each change in the email',
  'settings.notification.smtpInfo': 'Outbox: {host}:{port} · {user}',
  'settings.notification.smtpMissing': 'SMTP not configured — set your mailbox on the Price Alerts page',
  'settings.notification.smtpNoPassword': 'No authorization code set',
  'settings.notification.off': 'Notifications off',
  'settings.notification.on': 'Notifications on — waiting for the next refresh',
  'settings.notification.lastOk': 'Last email delivered ({time})',
  'settings.notification.lastFailed': 'Last email failed: {reason}',
  'settings.notification.lastRetryable': 'Temporary failure — will retry next refresh (attempt {n})',
  'settings.notification.lastPermanent': 'Gave up retrying — check mailbox and authorization code',
  'settings.notification.test': 'Send test email',
  'settings.notification.testSent': 'Test email sent — check your inbox',
  'settings.notification.stats':
    'Delivered {delivered} · failed {failed} ({retryable} can retry)',
}

export default settings
