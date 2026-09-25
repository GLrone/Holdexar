/* 更新模块文案：顶栏提示入口（components/business/UpdateEntry.vue）、
   更新报告窗口（components/business/UpdateDialog.vue）、更新内容渲染
   （components/business/UpdateNotes.vue）。设置页只剩「检查更新」入口。 */
export default {
  'updateDialog.title': '应用更新',
  'updateDialog.entry': '更新',
  'updateDialog.entryTip': '检测应用更新',
  'updateDialog.checkNow': '检测更新',
  'updateDialog.notesTitle': 'v{version} 更新内容',
  'updateDialog.notesEmpty': '此版本未提供更新说明',
  'updateDialog.releaseDate': '发布于 {date}',

  'updateDialog.headChecking': '正在检查更新',
  'updateDialog.headLatest': '已是最新版本',
  'updateDialog.headUnavailable': '检查更新失败',
  'updateDialog.headAvailable': '发现新版本 v{version}',
  'updateDialog.headDownloading': '正在下载 v{version}',
  'updateDialog.headReady': 'v{version} 已就绪',
  'updateDialog.headFailed': '更新失败',

  'updateDialog.checking': '正在检查更新…',
  'updateDialog.upToDate': '当前已是最新版本（v{version}）',
  'updateDialog.checkFailedHint':
    '检查更新失败，可能是网络不可达。可稍后重试，或到发布页手动下载。',
  'updateDialog.message':
    '当前 v{current}，可升级到 v{latest}。更新不会动游戏库、账号与设置数据。',
  'updateDialog.whatsNew': '更新内容',
  'updateDialog.packageSize': '更新包大小 {size}，下载期间可继续使用应用。',
  'updateDialog.autoDownload': '自动下载并安装更新',

  'updateDialog.progressLabel': '下载进度',
  'updateDialog.probe': '正在探测可用下载通道…',
  'updateDialog.verify': '正在校验文件…',
  'updateDialog.extract': '正在解包…',
  'updateDialog.eta': '剩余约 {seconds} 秒',
  'updateDialog.channel': '当前通道：{channel}',

  'updateDialog.failAssetMissing':
    '该版本还没有可下载的安装包（发布页上暂无此版本的资产）。请稍后再试，或到发布页手动下载。',
  'updateDialog.failNetwork':
    '所有下载通道都不可用。请检查网络/代理后重试，或到发布页手动下载。',
  'updateDialog.failVerify': '下载完成但校验未通过，已丢弃（不会安装坏包）。请重试或手动下载。',
  'updateDialog.failCancelled': '已取消下载，已下载的部分会保留，下次继续。',
  'updateDialog.failGeneric': '更新失败。可重试，或到发布页手动下载。',
  'updateDialog.readyBody': 'v{version} 已下载并通过校验，重启应用即可完成安装。',
  'updateDialog.readyHint': '重启期间界面会短暂关闭，随后自动回到新版；游戏库与账号数据不受影响。',

  'updateDialog.updateNow': '立即更新',
  'updateDialog.downloading': '下载中…',
  'updateDialog.retry': '重试',
  'updateDialog.cancel': '取消下载',
  'updateDialog.later': '稍后',
  'updateDialog.restart': '立即重启并更新',
  'updateDialog.manualDownload': '手动下载',
  'updateDialog.skip': '跳过此版本',

  'updateDialog.toastDownloadFailed': '下载启动失败：{error}',
  'updateDialog.toastRestartUnsupported': '浏览器模式无法自动重启，请手动关闭并重新打开应用',
  'updateDialog.toastRestartFailed': '重启失败，请手动关闭并重新打开应用',
  'updateDialog.toastCloseBlocked': '下载进行中，请先取消下载再关闭',
} as const