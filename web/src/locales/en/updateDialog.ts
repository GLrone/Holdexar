/* Update module strings: the top-bar entry (UpdateEntry.vue), the update report
   window (UpdateDialog.vue) and the release-notes renderer (UpdateNotes.vue). */
export default {
  'updateDialog.title': 'App Update',
  'updateDialog.entry': 'Update',
  'updateDialog.entryTip': 'Check for app updates',
  'updateDialog.checkNow': 'Check for updates',
  'updateDialog.notesTitle': "What's new in v{version}",
  'updateDialog.notesEmpty': 'This version ships no release notes',
  'updateDialog.releaseDate': 'Released {date}',

  'updateDialog.headChecking': 'Checking for updates',
  'updateDialog.headLatest': 'You are up to date',
  'updateDialog.headUnavailable': 'Update check failed',
  'updateDialog.headAvailable': 'New version v{version} is available',
  'updateDialog.headDownloading': 'Downloading v{version}',
  'updateDialog.headReady': 'v{version} is ready',
  'updateDialog.headFailed': 'Update failed',

  'updateDialog.checking': 'Checking for updates…',
  'updateDialog.upToDate': 'You are on the latest version (v{version})',
  'updateDialog.checkFailedHint':
    'Could not check for updates — the network may be unreachable. Retry later or download it manually from the releases page.',
  'updateDialog.message':
    'You are on v{current}; v{latest} is available. Updating keeps your library, accounts and settings intact.',
  'updateDialog.whatsNew': "What's new",
  'updateDialog.packageSize': 'Package size {size}. You can keep using the app while it downloads.',
  'updateDialog.autoDownload': 'Download and install updates automatically',

  'updateDialog.progressLabel': 'Download progress',
  'updateDialog.probe': 'Probing download channels…',
  'updateDialog.verify': 'Verifying download…',
  'updateDialog.extract': 'Extracting package…',
  'updateDialog.eta': 'about {seconds}s left',
  'updateDialog.channel': 'Channel: {channel}',

  'updateDialog.failAssetMissing':
    'No downloadable package exists for this version yet (the release has no asset). Try again later or download it manually.',
  'updateDialog.failNetwork':
    'Every download channel failed. Check your network/proxy and retry, or download manually.',
  'updateDialog.failVerify':
    'Download finished but failed verification and was discarded (a broken package is never installed). Retry or download manually.',
  'updateDialog.failCancelled': 'Download cancelled. The partial download is kept for next time.',
  'updateDialog.failGeneric': 'Update failed. Retry, or download manually from the releases page.',
  'updateDialog.readyBody': 'v{version} is downloaded and verified. Restart the app to install it.',
  'updateDialog.readyHint':
    'The window closes briefly during restart and comes back on the new version; your library and accounts are untouched.',

  'updateDialog.updateNow': 'Update now',
  'updateDialog.downloading': 'Downloading…',
  'updateDialog.retry': 'Retry',
  'updateDialog.cancel': 'Cancel download',
  'updateDialog.later': 'Later',
  'updateDialog.restart': 'Restart and update',
  'updateDialog.manualDownload': 'Manual download',
  'updateDialog.skip': 'Skip this version',

  'updateDialog.toastDownloadFailed': 'Could not start the download: {error}',
  'updateDialog.toastRestartUnsupported': 'Browser mode cannot restart the app; please reopen it manually',
  'updateDialog.toastRestartFailed': 'Restart failed; please close and reopen the app manually',
  'updateDialog.toastCloseBlocked': 'Download in progress — cancel it before closing',
} as const