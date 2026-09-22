<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { currencyName } from '@/api/currencies'
import {
  notificationsApi,
  settingsApi,
  systemApi,
  type NotificationPrefs,
  type NotificationPrefsUpdate,
  type NotificationStats,
  type SettingsPayload,
  type BackupItem,
  type SteamAccountItem,
} from '@/api/client'
import { useI18n, type MessageKey } from '@/locales'
import { useAccountStore } from '@/stores/account'
import { useRegionsStore } from '@/stores/regions'
import { useSettingsStore } from '@/stores/settings'
import { useTourStore } from '@/stores/tour'
import { useUpdaterStore } from '@/stores/updater'
import { friendCodeOf } from '@/utils/steamId'
import CurrencyFlag from '@/components/CurrencyFlag.vue'
import RegionFlag from '@/components/RegionFlag.vue'
import {
  HlButton, HlDialog, HlIcon, HlImg, HlInput, HlSkeleton, HlSwitch, message,
} from '@/components/ui'

const { t } = useI18n()

const loading = ref(true)
const saving = ref(false)
const errorMsg = ref('')

/* 更新检查结果与侧栏红点共用一份（App.vue 启动时也会查一次） */
const updaterStore = useUpdaterStore()

/* 产品导览手动重开（首次启动已自动弹过）：开的是 App.vue 里那个全局实例，
   本页不自己挂浮层——页面实例会被导览第一步的 router.push 卸载 */
const tour = useTourStore()

const steamId = ref('')
const apiKeyInput = ref('')
const apiKeyMasked = ref('')
const hasApiKey = ref(false)

/* ── Steam 多账户绑定（Cookie → 钱包余额 / 结算地区 / 愿望单与游戏计数）── */
const accountStore = useAccountStore()
/* 地区列截断后的 title 全名（区服名以 stores/regions 为单一来源） */
const regionsStore = useRegionsStore()
const cookieInput = ref('')
const cookieSaving = ref(false)

const accounts = computed(() => accountStore.accounts)
const activeAccount = computed(() => accountStore.active)
const hasCookie = computed(() => accountStore.status?.has_cookie ?? false)
const mismatch = computed(() => accountStore.status?.mismatch ?? false)

/** active 账号的钱包（顶层 wallet 与 active 一致） */
const wallet = computed(() => accountStore.status?.wallet ?? null)

/* 每账号同步状态：wallet_error 非空 = 最近一次同步失败（错误详情收进悬停
   提示，不占行内空间）；无钱包快照 = 尚未同步。wallet.checked_at 在失败时
   是失败尝试的时间，成功时是上次成功同步的时间。 */
type SyncTone = 'ok' | 'fail' | 'idle'
function syncStateOf(acc: SteamAccountItem): { tone: SyncTone; label: string; tip: string } {
  if (!acc.wallet) {
    return { tone: 'idle', label: t('settings.steam.syncIdle'), tip: t('settings.steam.syncTipIdle') }
  }
  const time = acc.wallet.checked_at ? acc.wallet.checked_at.replace('T', ' ').slice(0, 19) : ''
  const error = acc.wallet_error || acc.wallet.error || ''
  if (error) {
    return {
      tone: 'fail',
      label: t('settings.steam.syncFail'),
      tip: time ? t('settings.steam.syncTipFail', { time, error }) : t('settings.steam.syncFail'),
    }
  }
  return {
    tone: 'ok',
    label: t('settings.steam.syncOk'),
    tip: time ? t('settings.steam.syncTipOk', { time }) : t('settings.steam.syncOk'),
  }
}

/* 绑定态身份展示：好友码（SteamID64 换算，统一出口）；缺失/异常时退化 */
const boundIdentity = computed(() => {
  const fc = friendCodeOf(accountStore.status?.cookie_steam_id || '')
  return fc ? t('settings.steam.friendCode', { code: fc }) : t('settings.steam.friendCodeUnknown')
})

/* 绑定输入框默认聚焦「当前账号」；切换/删除即时生效 */
async function setActiveAccount(steamId: string) {
  if (steamId === activeAccount.value?.steam_id) return
  try {
    await accountStore.setActive(steamId)
    message.success(t('settings.toast.accountSwitched'))
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  }
}

async function removeAccount(steamId: string, label: string) {
  try {
    await accountStore.removeAccount(steamId)
    message.success(t('settings.toast.accountUnbound', { name: label }))
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  }
}

/* ── 数据备份（VACUUM INTO 在线快照：不占用库文件、含 WAL 已提交事务）── */
const backups = ref<BackupItem[]>([])
const backingUp = ref(false)
const verifyingName = ref('')
const restoringName = ref('')
const removingName = ref('')

function fmtSize(bytes: number): string {
  return bytes >= 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : `${(bytes / 1024).toFixed(0)} KB`
}

async function loadBackups() {
  try {
    const data = await systemApi.backupList()
    backups.value = data.items
  } catch {
    /* 列表失败不阻塞设置页 */
  }
}

async function createBackup() {
  backingUp.value = true
  try {
    const res = await systemApi.backupCreate()
    message.success(
      t('settings.toast.backupCreated', {
        name: res.name,
        size: fmtSize(res.sizeBytes),
        games: res.games,
      }),
    )
    await loadBackups()
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    backingUp.value = false
  }
}

async function verifyBackup(name: string) {
  verifyingName.value = name
  try {
    const res = await systemApi.backupVerify(name)
    if (res.integrityOk)
      message.success(t('settings.toast.backupVerified', { name, games: res.games }))
    else message.error(t('settings.toast.backupVerifyFailed', { name }))
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    verifyingName.value = ''
  }
}

/* 恢复两段确认：第一次点变红「确认恢复」，3s 内再点才执行（防误触） */
const restoreArmedName = ref('')
let restoreArmTimer: ReturnType<typeof setTimeout> | null = null

function armRestore(name: string) {
  restoreArmedName.value = name
  if (restoreArmTimer) clearTimeout(restoreArmTimer)
  restoreArmTimer = setTimeout(() => (restoreArmedName.value = ''), 3000)
}

async function restoreBackup(name: string) {
  if (restoreArmedName.value !== name) {
    armRestore(name)
    return
  }
  if (restoreArmTimer) clearTimeout(restoreArmTimer)
  restoreArmedName.value = ''
  restoringName.value = name
  try {
    const res = await systemApi.backupRestore(name)
    if (res.restored) {
      message.success(t('settings.toast.backupRestored', { name }))
      await load()
    }
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    restoringName.value = ''
  }
}

async function removeBackup(name: string) {
  removingName.value = name
  try {
    await systemApi.backupRemove(name)
    message.success(t('settings.toast.backupRemoved', { name }))
    await loadBackups()
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    removingName.value = ''
  }
}

/* ── 应用更新入口 ──
 * 检查/下载/校验/重启全部在全局更新弹窗（components/business/UpdateDialog.vue）
 * 里闭环；本页只保留「当前版本 + 入口按键」。数据目录在换装白名单内永不移动。
 * 检查结果存在 stores/updater（启动时也会查一次供侧栏红点用），本页与弹窗
 * 读写同一份，不会「这边检查了那边不知道」。 */
const appVersion = ref('')
const repoSlug = ref('')
const updateInfo = computed(() => updaterStore.info)
const updateChecking = computed(() => updaterStore.checking)
const updatePendingTag = computed(() => updaterStore.pendingTag)

/** 发布页地址（仓库标识由后端 /system/info 下发，前端不硬编码） */
const releasesUrl = computed(() =>
  repoSlug.value ? `https://github.com/${repoSlug.value}/releases` : '',
)

function openUpdateDialog() {
  void updaterStore.openDialog()
}

/* ── 更新行为开关（新版本提示 / 静默自动更新）──
 * 状态放 settings store（启动逻辑 App.vue 与侧栏红点都读同一份），本页只读写。
 * null（未拉到）按「提示开 / 静默关」处理——与后端默认值一致，UI 不会闪错态。 */
const settingsStore = useSettingsStore()
const updateNotifyOn = computed(() => settingsStore.updateNotify !== false)
const updateAutoOn = computed(() => settingsStore.updateAuto === true)
const updatePrefsToggling = ref(false)

async function toggleUpdateNotify(on: boolean) {
  if (updatePrefsToggling.value) return
  updatePrefsToggling.value = true
  try {
    const ok = await settingsStore.setUpdateNotify(on)
    if (ok) message.success(t(on ? 'settings.update.notifyOn' : 'settings.update.notifyOff'))
    else message.error(t('settings.update.switchFailed'))
  } finally {
    updatePrefsToggling.value = false
  }
}

async function toggleUpdateAuto(on: boolean) {
  if (updatePrefsToggling.value) return
  updatePrefsToggling.value = true
  // setUpdateAuto 内部吞异常并返回成败，这里不必再包一层 try
  const ok = await settingsStore.setUpdateAuto(on)
  updatePrefsToggling.value = false
  if (!ok) {
    message.error(t('settings.update.switchFailed'))
    return
  }
  message.success(t(on ? 'settings.update.autoOn' : 'settings.update.autoOff'))
  if (!on) return
  /* 开启即兑现一次：立刻强制查一遍并起后台下载，而不是等下次启动。
     check(true) 必须强制——两个开关此前都关时启动不查，本地可能压根没有结果。 */
  void (async () => {
    const res = await updaterStore.check(true)
    if (!res?.available || !res.latest || updaterStore.pendingTag) return
    await updaterStore.download()
    message.info(t('settings.update.autoStarted', { version: res.latest }))
  })()
}

async function load() {
  loading.value = true
  errorMsg.value = ''
  try {
    const data: SettingsPayload = await settingsApi.get()
    steamId.value = data.account.steam_id
    apiKeyMasked.value = data.account.steam_api_key
    hasApiKey.value = data.account.has_api_key
    await accountStore.load()
    await loadBackups()
    /* 版本信息 + 与后端对齐更新状态（暂存/进行中的下载；失败静默不阻塞设置页） */
    try {
      const info = await systemApi.info()
      appVersion.value = info.version
      repoSlug.value = info.repo
      await updaterStore.sync()
    } catch {
      /* 更新状态拉不到不影响设置页 */
    }
  } catch (e) {
    errorMsg.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function save() {
  saving.value = true
  errorMsg.value = ''
  try {
    const key = apiKeyInput.value.trim()
    const data = await settingsApi.update({
      account: {
        steam_id: steamId.value.trim(),
        ...(key ? { steam_api_key: key } : {}),
      },
    })
    apiKeyMasked.value = data.account.steam_api_key
    hasApiKey.value = data.account.has_api_key
    apiKeyInput.value = ''
    message.success(t('settings.toast.accountSaved'))
  } catch (e) {
    errorMsg.value = e instanceof Error ? e.message : String(e)
  } finally {
    saving.value = false
  }
}

/* 粘贴归一化：兼容整行 "Cookie: ..." 复制、换行分隔、多余空白
   （后端只按 "; " 切分，这里统一成该形态再上传） */
function normalizeCookieRaw(raw: string): string {
  const text = raw.trim().replace(/^cookie\s*:\s*/i, '')
  return text
    .split(/\r?\n|;/)
    .map((s) => s.trim())
    .filter((s) => s && s.includes('='))
    .join('; ')
}

/* 绑定结果统一汇报（手动粘贴与自动抓取共用） */
function reportBindResult() {
  const status = accountStore.status
  if (status?.mismatch) {
    message.error(status.message || t('settings.toast.cookieMismatch'))
  } else if (status?.wallet?.check_ok) {
    message.success(t('settings.toast.bindSuccess', { balance: status.wallet.balance_display }))
  } else if (status?.sync_error) {
    message.warning(t('settings.toast.bindSyncFailed', { error: status.sync_error }))
  } else {
    message.success(t('settings.toast.cookieSaved'))
  }
}

/* ── 绑定风险弹窗 ────────────────────────────────────────────
   每次绑定动作（手动粘贴 / 桌面端一键登录）都会先弹，确定键带 5s 倒计时
   强制停留阅读；确定后才继续原流程——'auto' 才是「后续的登录窗口」
   （桌面端 start_steam_login 子窗口），'manual' 继续提交已粘贴的 Cookie。 */
type RiskNextAction = 'auto' | 'manual'
const RISK_CONFIRM_DELAY_SECONDS = 5

/* 弹窗正文条目：每条 = 加粗关键词(lead) + 短说明(rest)，扫加粗词即可抓住重点；
   首条（凭据明文保存）是核心风险，danger 色强调。常量只存词条 key 不存译文
   （模块常量只求值一次，存译文会把语言冻在加载那一刻）。 */
interface RiskItem {
  lead: MessageKey
  rest: MessageKey
  danger?: boolean
}
const RISK_ITEMS: RiskItem[] = [
  { lead: 'settings.risk.item1Lead', rest: 'settings.risk.item1Rest', danger: true },
  { lead: 'settings.risk.item2Lead', rest: 'settings.risk.item2Rest' },
  { lead: 'settings.risk.item3Lead', rest: 'settings.risk.item3Rest' },
  { lead: 'settings.risk.item4Lead', rest: 'settings.risk.item4Rest' },
]
const LEAK_ITEMS: RiskItem[] = [
  { lead: 'settings.risk.leak1Lead', rest: 'settings.risk.leak1Rest' },
  { lead: 'settings.risk.leak2Lead', rest: 'settings.risk.leak2Rest' },
  { lead: 'settings.risk.leak3Lead', rest: 'settings.risk.leak3Rest' },
  { lead: 'settings.risk.leak4Lead', rest: 'settings.risk.leak4Rest' },
]
const riskDialogOpen = ref(false)
const riskNextAction = ref<RiskNextAction>('manual')
const riskCountdown = ref(RISK_CONFIRM_DELAY_SECONDS)
let riskTimer: ReturnType<typeof setInterval> | null = null

function stopRiskCountdown() {
  if (riskTimer !== null) {
    clearInterval(riskTimer)
    riskTimer = null
  }
}

function startRiskCountdown() {
  stopRiskCountdown()
  riskCountdown.value = RISK_CONFIRM_DELAY_SECONDS
  riskTimer = setInterval(() => {
    riskCountdown.value--
    if (riskCountdown.value <= 0) stopRiskCountdown()
  }, 1000)
}

function openRiskDialog(action: RiskNextAction) {
  riskNextAction.value = action
  riskDialogOpen.value = true
  startRiskCountdown()
}

function riskConfirm() {
  if (riskCountdown.value > 0) return
  riskDialogOpen.value = false
  if (riskNextAction.value === 'auto') void autoFetchCookie()
  else void bindCookie()
}

watch(riskDialogOpen, (open) => {
  if (!open) stopRiskCountdown()
})
onBeforeUnmount(stopRiskCountdown)

async function bindCookie() {
  const raw = normalizeCookieRaw(cookieInput.value)
  if (!raw) {
    message.warning(t('settings.toast.cookieEmpty'))
    return
  }
  cookieSaving.value = true
  try {
    await accountStore.bindCookies(raw)
    cookieInput.value = ''
    reportBindResult()
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    cookieSaving.value = false
  }
}

/* 桌面端自动抓取：弹出 Steam 登录子窗口，登录成功后由 pywebview
   读取 WebView2 Cookie（含 httpOnly 的 steamLoginSecure）自动回传。
   steam_id/is_new 是桌面端从 steamLoginSecure 解析的账号判定结果
   （登录的是哪个号 / 新账号还是已有账号换绑）。 */
type SteamLoginResult = { ok: boolean; cookies?: string; error?: string; steam_id?: string; is_new?: boolean }
const desktopApi = (): { startSteamLogin?: () => Promise<SteamLoginResult> } | undefined =>
  (window as unknown as { pywebview?: { api?: { start_steam_login?: () => Promise<SteamLoginResult> } } })
    .pywebview?.api

async function autoFetchCookie() {
  const api = desktopApi()
  if (!api?.start_steam_login) {
    message.info(t('settings.toast.desktopOnly'))
    return
  }
  cookieSaving.value = true
  message.info(t('settings.toast.loginOpened'))
  try {
    const res = await api.start_steam_login()
    if (res?.ok && res.cookies) {
      // 已识别身份时先亮"是哪个号"（新账号 vs 换绑），绑定结果随后汇报
      if (res.steam_id) {
        const shortId = res.steam_id.slice(-10)
        message.info(
          res.is_new === false
            ? t('settings.toast.rebindDetected', { id: shortId })
            : t('settings.toast.accountRecognized', { id: shortId }),
        )
      }
      await accountStore.bindCookies(normalizeCookieRaw(res.cookies))
      reportBindResult()
    } else {
      message.warning(res?.error || t('settings.toast.cookieFetchFailed'))
    }
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    cookieSaving.value = false
  }
}

async function resyncWallet() {
  cookieSaving.value = true
  message.loading(t('settings.toast.fetching'))
  try {
    await accountStore.sync()
    if (accountStore.status?.wallet?.check_ok) {
      message.success(t('settings.toast.walletRefreshed'))
    } else {
      message.error(accountStore.status?.sync_error || t('settings.toast.walletRefreshFailed'))
    }
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    cookieSaving.value = false
  }
}

function openSteamidIo() {
  window.open('https://steamid.io', '_blank', 'noopener noreferrer')
}

// ─── 价格事件通知 ─────────────────────────────────────────────
// 类别是用户面分类（价格变化 / 历史低价 / 可购买状态 / 免费与下架），
// 内部事件枚举不出现在界面上。通知默认关闭：SMTP 配好也不会自动开。

const notifyPrefs = ref<NotificationPrefs | null>(null)
const notifyStats = ref<NotificationStats | null>(null)
const notifyBusy = ref(false)
const notifyTesting = ref(false)

async function loadNotifications() {
  try {
    const [prefs, stats] = await Promise.all([
      notificationsApi.prefs(),
      notificationsApi.stats(),
    ])
    notifyPrefs.value = prefs
    notifyStats.value = stats
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  }
}

async function patchNotifications(patch: NotificationPrefsUpdate) {
  notifyBusy.value = true
  try {
    await notificationsApi.updatePrefs(patch)
    await loadNotifications()
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
    // 失败回读：不让开关停在错的那一侧
    await loadNotifications()
  } finally {
    notifyBusy.value = false
  }
}

/** 通知是否真的可用——开关开了但出口没配好，界面必须说清楚 */
const notifyReady = computed(() => !!notifyPrefs.value?.smtp.configured)

const notifyStateKey = computed<MessageKey>(() => {
  const prefs = notifyPrefs.value
  if (!prefs) return 'settings.notification.off'
  if (!prefs.smtp.configured) return 'settings.notification.smtpMissing'
  if (!prefs.enabled) return 'settings.notification.off'
  const last = prefs.lastDelivery
  if (last?.status === 'delivered') return 'settings.notification.lastOk'
  if (last?.status === 'failed') {
    return last.retryable
      ? 'settings.notification.lastRetryable'
      : 'settings.notification.lastPermanent'
  }
  return 'settings.notification.on'
})

const notifyStateParams = computed<Record<string, string | number>>(() => {
  const last = notifyPrefs.value?.lastDelivery
  if (!last) return {}
  return {
    time: last.at ? last.at.slice(5, 16).replace('T', ' ') : '—',
    reason: last.reason ?? '',
    n: last.attempts ?? 0,
  }
})

async function sendTestNotification() {
  notifyTesting.value = true
  try {
    await notificationsApi.test()
    message.success(t('settings.notification.testSent'))
  } catch (e) {
    // 400 = SMTP 配置 / 凭据错误：必须让用户看到，不能显示成功
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    notifyTesting.value = false
  }
}

onMounted(() => {
  /* 更新开关存在 settings store（启动逻辑与侧栏红点共用）：本页也要保证它拉到过
     ——深链直接进本页时 App 外壳虽会 load，但失败重试的兜底放在这里更稳。 */
  void settingsStore.load()
  void load()
  void loadNotifications()
})
</script>

<template>
  <section class="settings-page">
    <div v-if="errorMsg" class="card" style="padding: 12px 16px; border-color: var(--danger-a40)">
      <span class="tag tag--danger">{{ errorMsg }}</span>
    </div>

    <div v-if="loading" class="hl-loading-pane">
      <HlSkeleton variant="text" :count="1" :rows="6" />
    </div>

    <template v-else>
      <!-- Steam 账户绑定（Cookie → 钱包/结算区） -->
      <div class="card settings-card" data-section="settings.section.steamAccount">
        <div class="section-title">{{ t('settings.section.steamAccount') }}</div>
        <div class="section-desc">{{ t('settings.steam.desc') }}</div>

        <div class="settings-row">
          <label class="hl-form-label">Steam Cookie</label>
          <div class="settings-row__line">
            <el-input
              v-model="cookieInput"
              type="password"
              show-password
              :placeholder="
                hasCookie
                  ? t('settings.steam.cookiePlaceholderBound', { identity: boundIdentity })
                  : t('settings.steam.cookiePlaceholder')
              "
              style="max-width: 420px"
            />
            <!-- 桌面端一键登录抓取：风险须知确认后才打开 Steam 登录子窗口 -->
            <HlButton art="outline" tone="blue" size="sm" :disabled="cookieSaving" :loading="cookieSaving" @click="openRiskDialog('auto')">
              <HlIcon v-if="!cookieSaving" name="zap" />
              {{ t('settings.steam.autoFetch') }}
            </HlButton>
            <HlButton
              v-if="!hasCookie"
              art="outline"
              tone="green"
              size="sm"
              :disabled="cookieSaving"
              :loading="cookieSaving"
              @click="openRiskDialog('manual')"
            >
              <HlIcon v-if="!cookieSaving" name="check" />
              {{ t('settings.steam.bind') }}
            </HlButton>
            <HlButton
              v-else
              art="outline"
              tone="green"
              size="sm"
              :disabled="cookieSaving"
              :loading="cookieSaving"
              @click="openRiskDialog('manual')"
            >
              <HlIcon v-if="!cookieSaving" name="check" />
              {{ t('settings.steam.addAccount') }}
            </HlButton>
          </div>

          <!-- 获取 Cookie 分步引导（默认收起） -->
          <details class="cookie-guide">
            <summary>{{ t('settings.steam.guideSummary') }}</summary>
            <ol class="cookie-guide__steps">
              <li>
                {{ t('settings.steam.guideStep1Pre') }}
                <a href="https://store.steampowered.com/login/" target="_blank" rel="noreferrer">store.steampowered.com</a>
                {{ t('settings.steam.guideStep1Post') }}
              </li>
              <!-- 第 2–4 步整步一条词条，行内 <kbd>/<code>/<b> 写在值里、由 v-html 渲染
                   （同 bundles.calc.excludeHint 的先例）。按元素边界切片会逼译文把被强调
                   的词钉死在原位，英文拼出来是残句。词条是应用自有静态文案（非用户输入），
                   v-html 无注入面；注入节点拿不到 scoped 属性，故下方 kbd/code 用 :deep()。 -->
              <li v-html="t('settings.steam.guideStep2')"></li>
              <li v-html="t('settings.steam.guideStep3')"></li>
              <li v-html="t('settings.steam.guideStep4')"></li>
            </ol>
            <div class="cookie-guide__notes">{{ t('settings.steam.guideNotes') }}</div>
          </details>
        </div>

        <!-- 绑定后展示：多账号列表（每个账号完整信息） -->
        <div v-if="hasCookie" class="account-summary">
          <div class="account-summary__head">
            <HlButton art="outline" tone="blue" size="sm" :disabled="cookieSaving" :loading="cookieSaving" @click="resyncWallet">
              <HlIcon v-if="!cookieSaving" name="refresh" />
              {{ t('settings.steam.refreshBalance') }}
            </HlButton>
          </div>
          <div v-if="mismatch" class="account-summary__warn">
            {{ t('settings.steam.mismatchWarn') }}
          </div>

          <div class="account-list">
            <div
              v-for="acc in accounts"
              :key="acc.steam_id"
              class="account-row"
              :class="{ 'is-active': acc.is_active }"
            >
              <!-- 头像 / 昵称 / 标记 -->
              <HlImg class="account-row__avatar" :src="acc.avatar_url" alt="">
                <template #fallback>
                  <div class="account-row__avatar account-row__avatar--fallback">
                    {{ (acc.persona_name || acc.friend_code || '?').slice(0, 1) }}
                  </div>
                </template>
              </HlImg>
              <div class="account-row__id">
                <div class="account-row__name">
                  <!-- 昵称列定宽：超长昵称在本列内截断（title 悬停看全名），
                       不允许撑列——昵称长短是跨行统计列漂移的源头之一 -->
                  <span class="account-row__name-text" :title="acc.persona_name || ''">{{
                    acc.persona_name || t('settings.steam.noNickname')
                  }}</span>
                  <span v-if="acc.is_primary" class="account-badge account-badge--primary">{{ t('settings.steam.primary') }}</span>
                  <span v-if="acc.is_active" class="account-badge account-badge--active">{{ t('settings.steam.active') }}</span>
                  <!-- 同步状态标记：悬停看最近同步时间（失败时附错误详情） -->
                  <span
                    class="account-badge"
                    :class="`account-badge--sync-${syncStateOf(acc).tone}`"
                    :title="syncStateOf(acc).tip"
                  >{{ syncStateOf(acc).label }}</span>
                </div>
                <div class="account-row__friend">{{ t('settings.steam.friendCode', { code: acc.friend_code || t('settings.steam.unknown') }) }}</div>
              </div>

              <!-- 钱包 / 币种 / 地区 / 计数 / 激活配额 -->
              <div class="account-row__stats">
                <div class="account-stat">
                  <span class="account-stat__label">{{ t('settings.steam.balance') }}</span>
                  <span class="account-stat__value account-stat__value--balance">
                    {{ acc.wallet?.balance_display ?? '—' }}
                  </span>
                </div>
                <div class="account-stat">
                  <span class="account-stat__label">{{ t('settings.steam.currency') }}</span>
                  <CurrencyFlag
                    v-if="acc.wallet?.currency_code"
                    :code="acc.wallet.currency_code"
                    :title="currencyName(acc.wallet.currency_code)"
                  />
                  <span v-else class="account-stat__value">—</span>
                </div>
                <div class="account-stat">
                  <span class="account-stat__label">{{ t('settings.steam.region') }}</span>
                  <RegionFlag
                    v-if="acc.wallet?.region_code"
                    :code="acc.wallet.region_code"
                    compact
                    :title="regionsStore.regionName(acc.wallet.region_code)"
                  />
                  <span v-else class="account-stat__value">—</span>
                </div>
                <div class="account-stat">
                  <span class="account-stat__label">{{ t('settings.steam.games') }}</span>
                  <span class="account-stat__value">{{ acc.game_count || '—' }}</span>
                </div>
                <div class="account-stat">
                  <span class="account-stat__label">{{ t('settings.steam.wishlist') }}</span>
                  <span class="account-stat__value">{{ acc.wishlist_count || '—' }}</span>
                </div>
                <div class="account-stat">
                  <span class="account-stat__label">{{ t('settings.steam.redeems') }}</span>
                  <span class="account-stat__value">{{ acc.redeem_used }}/10</span>
                </div>
              </div>

              <!-- 行操作 -->
              <div class="account-row__actions">
                <HlButton
                  v-if="!acc.is_active"
                  variant="text"
                  size="sm"
                  :disabled="accountStore.switching"
                  :loading="accountStore.switching"
                  @click="setActiveAccount(acc.steam_id)"
                >
                  {{ t('settings.steam.setActive') }}
                </HlButton>
                <HlButton
                  variant="text"
                  size="sm"
                  tone="red"
                  @click="removeAccount(acc.steam_id, acc.persona_name || acc.friend_code)"
                >
                  {{ t('settings.steam.unbind') }}
                </HlButton>
              </div>
            </div>
          </div>

          <div v-if="wallet?.checked_at" class="account-summary__meta">
            {{
              t('settings.steam.syncMeta', {
                time: wallet.checked_at.replace('T', ' ').slice(0, 19),
              })
            }}
          </div>
        </div>
      </div>

      <!-- 账户 -->
      <div class="card settings-card" data-section="settings.section.account">
        <div class="section-title">{{ t('settings.section.account') }}</div>
        <div class="section-desc">{{ t('settings.account.desc') }}</div>

        <div class="settings-row">
          <label class="hl-form-label">SteamID64</label>
          <div class="settings-row__line">
            <el-input
              v-model="steamId"
              :placeholder="t('settings.account.steamIdPlaceholder')"
              clearable
              style="max-width: 420px"
            />
            <!-- 艺术按键方案二：查询（蓝） -->
            <HlButton art="outline" tone="blue" size="sm" @click="openSteamidIo">
              {{ t('settings.account.lookup') }}
            </HlButton>
          </div>
        </div>

        <div class="settings-row">
          <label class="hl-form-label">
            Steam Web API Key
            <span class="section-desc" style="display: inline; margin-left: 8px">
              <a
                href="https://steamcommunity.com/dev/apikey"
                target="_blank"
                rel="noreferrer"
                style="color: var(--accent)"
                >{{ t('settings.account.applyFree') }}</a
              >
              · {{ t('settings.account.apiKeyHint') }}
            </span>
          </label>
          <div class="settings-row__line">
            <el-input
              v-model="apiKeyInput"
              type="password"
              show-password
              :placeholder="
                hasApiKey
                  ? t('settings.account.apiKeyPlaceholderSet', { mask: apiKeyMasked })
                  : t('settings.account.apiKeyPlaceholder')
              "
              style="max-width: 420px"
            />
            <!-- 艺术按键方案二：保存（绿），回到输入行内不再游离 -->
            <HlButton art="outline" tone="green" size="sm" :disabled="saving" :loading="saving" @click="save">
              <HlIcon v-if="!saving" name="check" />
              {{ t('settings.account.save') }}
            </HlButton>
          </div>
        </div>
      </div>

      <!-- 数据备份（VACUUM INTO 在线快照） -->
      <div class="card settings-card" data-section="settings.section.backup">
        <div class="section-title">{{ t('settings.section.backup') }}</div>
        <div class="section-desc">{{ t('settings.backup.desc') }}</div>

        <div class="settings-row">
          <div class="settings-row__line">
            <HlButton art="outline" tone="blue" size="sm" :disabled="backingUp" :loading="backingUp" @click="createBackup">
              <HlIcon v-if="!backingUp" name="plus" />
              {{ backingUp ? t('settings.backup.snapshotting') : t('settings.backup.createNow') }}
            </HlButton>
            <span class="section-desc" style="display: inline; margin-left: 8px">
              {{ t('settings.backup.count', { n: backups.length }) }}
            </span>
          </div>
        </div>

        <div v-if="backups.length" class="settings-backup-list">
          <div v-for="b in backups" :key="b.name" class="settings-backup-item">
            <span style="min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: var(--font-mono, monospace); font-size: 12px">
              {{ b.name }}
            </span>
            <span class="section-desc" style="flex-shrink: 0">{{ fmtSize(b.sizeBytes) }}</span>
            <span class="section-desc" style="flex-shrink: 0">
              {{ b.createdAt.replace('T', ' ').slice(0, 19) }}
            </span>
            <span class="settings-backup-actions">
              <HlButton
                variant="text"
                size="sm"
                :disabled="verifyingName === b.name"
                :loading="verifyingName === b.name"
                @click="verifyBackup(b.name)"
              >
                {{ verifyingName === b.name ? t('settings.backup.verifying') : t('settings.backup.verify') }}
              </HlButton>
              <a
                class="settings-backup-download"
                :href="`/api/v1/system/backup/${encodeURIComponent(b.name)}/download`"
                download
                >{{ t('settings.backup.download') }}</a
              >
              <HlButton
                variant="text"
                size="sm"
                :disabled="restoringName === b.name || backingUp"
                :loading="restoringName === b.name"
                :class="{ 'settings-backup-armed': restoreArmedName === b.name }"
                @click="restoreBackup(b.name)"
              >
                {{
                  restoringName === b.name
                    ? t('settings.backup.restoring')
                    : restoreArmedName === b.name
                      ? t('settings.backup.confirmRestore')
                      : t('settings.backup.restore')
                }}
              </HlButton>
              <HlButton
                variant="text"
                size="sm"
                :disabled="removingName === b.name"
                :loading="removingName === b.name"
                @click="removeBackup(b.name)"
              >
                {{ removingName === b.name ? t('settings.backup.removing') : t('settings.backup.remove') }}
              </HlButton>
            </span>
          </div>
        </div>
        <div v-else class="section-desc">{{ t('settings.backup.empty') }}</div>
      </div>

      <!-- 应用更新（GitHub Releases：下载暂存 + 重启换装，数据目录永不移动） -->
      <div class="card settings-card" data-section="settings.section.update">
        <div class="section-title">{{ t('settings.section.update') }}</div>
        <div class="section-desc">{{ t('settings.update.desc') }}</div>

        <!-- 更新交互全在全局弹窗（模糊幕布）里闭环：本页只留入口 + 状态一句。
             进度/下载/重启不再出现在这里——它们是应用级事务，塞在页签里时
             用户切走再回来就得靠 store 续命，体感就是「点了没反应」。 -->
        <div class="settings-row">
          <div class="settings-row__line">
            <HlButton art="outline" tone="blue" size="sm" :loading="updateChecking" @click="openUpdateDialog">
              <HlIcon v-if="!updateChecking" name="refresh" />
              {{ updateChecking ? t('settings.update.checking') : t('settings.update.check') }}
            </HlButton>
            <span class="section-desc" style="display: inline; margin-left: 8px">
              {{ t('settings.update.currentVersion', { version: appVersion || '…' }) }}
            </span>
          </div>
        </div>

        <!-- 更新行为开关：提示 / 静默自动更新。两个开关独立——提示管「告知」，
             静默管「后台下载」；静默开着时，提示只负责下载完成后的重启提醒。 -->
        <div class="settings-row">
          <div class="settings-row__line">
            <HlSwitch
              :model-value="updateNotifyOn"
              accent
              :disabled="updatePrefsToggling"
              :label="t('settings.update.notifyLabel')"
              :title="t('settings.update.notifyHint')"
              @update:model-value="toggleUpdateNotify"
            />
          </div>
          <div class="section-desc">{{ t('settings.update.notifyHint') }}</div>
        </div>

        <div class="settings-row">
          <div class="settings-row__line">
            <HlSwitch
              :model-value="updateAutoOn"
              accent
              :disabled="updatePrefsToggling"
              :label="t('settings.update.autoLabel')"
              :title="t('settings.update.autoHint')"
              @update:model-value="toggleUpdateAuto"
            />
          </div>
          <div class="section-desc">{{ t('settings.update.autoHint') }}</div>
        </div>

        <div v-if="updatePendingTag" class="settings-update-ready">
          <div class="settings-update-ready__text">
            {{ t('settings.update.pendingReady', { version: updatePendingTag.replace(/^v/, '') }) }}
          </div>
          <div class="settings-row__line">
            <HlButton art="combo" tone="green" size="sm" @click="openUpdateDialog">
              <HlIcon name="check" />
              {{ t('settings.update.restartNow') }}
            </HlButton>
          </div>
        </div>

        <div v-else-if="updateInfo?.available" class="section-desc">
          {{ t('settings.update.availableHint', { version: updateInfo.latest }) }}
        </div>
        <div v-else-if="updateInfo?.reason === 'network'" class="section-desc">
          {{ t('settings.update.networkPre') }}
          <a :href="releasesUrl" target="_blank">{{ t('settings.update.releasesPage') }}</a>{{ t('settings.update.networkPost') }}
        </div>
        <div v-else-if="updateInfo" class="section-desc">
          {{ t('settings.update.upToDate', { version: updateInfo.current }) }}
        </div>
      </div>

      <!-- 价格事件通知：总开关 + 用户类别 + 静默窗 + 出口状态 + 测试邮件。
           类别是用户面分类，内部事件枚举不出现在这里。 -->
      <div class="card settings-card" data-section="settings.section.notification">
        <div class="section-title">{{ t('settings.section.notification') }}</div>
        <div class="section-desc">{{ t('settings.notification.desc') }}</div>

        <div class="settings-row">
          <div class="settings-row__line">
            <HlSwitch
              :model-value="!!notifyPrefs?.enabled"
              accent
              :disabled="notifyBusy || !notifyPrefs"
              :label="t('settings.notification.enabledLabel')"
              @update:model-value="(on: boolean) => patchNotifications({ enabled: on })"
            />
          </div>
          <div class="section-desc">{{ t('settings.notification.enabledHint') }}</div>
        </div>

        <div class="settings-row">
          <div class="section-desc notify-cat-label">
            {{ t('settings.notification.categoriesLabel') }}
          </div>
          <div class="settings-row__line notify-cats">
            <HlSwitch
              v-for="cat in notifyPrefs?.categories || []"
              :key="cat.key"
              :model-value="cat.enabled"
              accent
              :disabled="notifyBusy || !notifyPrefs?.enabled"
              :label="cat.label"
              @update:model-value="
                (on: boolean) => patchNotifications({ categories: { [cat.key]: on } })
              "
            />
          </div>
          <div class="section-desc">{{ t('settings.notification.categoriesHint') }}</div>
        </div>

        <div class="settings-row">
          <div class="settings-row__line">
            <HlSwitch
              :model-value="!!notifyPrefs?.quietEnabled"
              accent
              :disabled="notifyBusy || !notifyPrefs?.enabled"
              :label="t('settings.notification.quietLabel')"
              @update:model-value="(on: boolean) => patchNotifications({ quietEnabled: on })"
            />
          </div>
          <div v-if="notifyPrefs?.quietEnabled" class="settings-row__line notify-times">
            <HlInput
              :model-value="notifyPrefs?.quietStart || '23:00'"
              type="time"
              :disabled="notifyBusy"
              @update:model-value="(v: string) => patchNotifications({ quietStart: v })"
            />
            <span class="notify-times__sep">–</span>
            <HlInput
              :model-value="notifyPrefs?.quietEnd || '08:00'"
              type="time"
              :disabled="notifyBusy"
              @update:model-value="(v: string) => patchNotifications({ quietEnd: v })"
            />
          </div>
          <div class="section-desc">{{ t('settings.notification.quietHint') }}</div>
        </div>

        <div class="settings-row">
          <div class="settings-row__line">
            <HlSwitch
              :model-value="!!notifyPrefs?.includeDetails"
              accent
              :disabled="notifyBusy || !notifyPrefs?.enabled"
              :label="t('settings.notification.detailsLabel')"
              @update:model-value="(on: boolean) => patchNotifications({ includeDetails: on })"
            />
          </div>
        </div>

        <!-- 状态：出口是否可用 + 最近一次投递结果（不展示任何凭据） -->
        <div class="settings-row">
          <div class="notify-status" :class="{ 'is-bad': !notifyReady }">
            <span class="notify-status__dot" />
            <span>{{ t(notifyStateKey, notifyStateParams) }}</span>
          </div>
          <div v-if="notifyPrefs?.smtp.configured" class="section-desc">
            {{ t('settings.notification.smtpInfo', {
              host: notifyPrefs.smtp.host,
              port: notifyPrefs.smtp.port,
              user: notifyPrefs.smtp.userMasked,
            }) }}
            <template v-if="!notifyPrefs.smtp.hasPassword">
              · {{ t('settings.notification.smtpNoPassword') }}
            </template>
          </div>
          <div v-if="notifyStats && notifyStats.total > 0" class="section-desc">
            {{ t('settings.notification.stats', {
              delivered: notifyStats.delivered,
              failed: notifyStats.failed,
              retryable: notifyStats.retryable,
            }) }}
          </div>
        </div>

        <div class="settings-row">
          <div class="settings-row__line">
            <HlButton
              art="outline"
              tone="green"
              size="sm"
              :loading="notifyTesting"
              :disabled="notifyBusy"
              @click="sendTestNotification"
            >
              {{ t('settings.notification.test') }}
            </HlButton>
          </div>
        </div>
      </div>

      <!-- 产品导览：首次启动自动展示过，这里手动重开（幂等） -->
      <div class="card settings-card" data-section="settings.section.tour">
        <div class="section-title">{{ t('settings.section.tour') }}</div>
        <div class="section-desc">{{ t('settings.tour.desc') }}</div>
        <HlButton size="sm" @click="tour.show()">{{ t('settings.tour.replay') }}</HlButton>
      </div>
    </template>

    <!-- 绑定风险弹窗：每次绑定动作（手动/自动）都会弹出，确定键 5s 倒计时；
         确定后才继续——'auto' 打开 Steam 登录子窗口，'manual' 提交已粘贴的 Cookie -->
    <HlDialog
      v-model="riskDialogOpen"
      :title="t('settings.risk.title')"
      :width="520"
      :mask-closable="false"
    >
      <div class="risk-body">
        <div class="risk-body__label">{{ t('settings.risk.bodyTitle') }}</div>
        <ul class="risk-body__list">
          <li v-for="it in RISK_ITEMS" :key="it.lead" :class="{ 'is-danger': it.danger }">
            <span class="risk-body__lead">{{ t(it.lead) }}</span>
            <span class="risk-body__rest">{{ t(it.rest) }}</span>
          </li>
        </ul>
        <div class="risk-body__label">{{ t('settings.risk.leakTitle') }}</div>
        <ol class="risk-body__list">
          <li v-for="it in LEAK_ITEMS" :key="it.lead">
            <span class="risk-body__lead">{{ t(it.lead) }}</span>
            <span class="risk-body__rest">{{ t(it.rest) }}</span>
          </li>
        </ol>
      </div>
      <template #footer>
        <HlButton variant="text" size="sm" @click="riskDialogOpen = false">
          {{ t('common.cancel') }}
        </HlButton>
        <HlButton art="outline" tone="green" size="sm" :disabled="riskCountdown > 0" @click="riskConfirm">
          <HlIcon v-if="riskCountdown <= 0" name="check" />
          {{
            riskCountdown > 0
              ? t('settings.risk.countdown', { n: riskCountdown })
              : t('settings.risk.confirm')
          }}
        </HlButton>
      </template>
    </HlDialog>
  </section>
</template>

<style scoped>
/* ── 绑定风险弹窗正文 ── */
.risk-body {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
  line-height: 1.7;
  color: var(--text-secondary);
}

.risk-body__label {
  margin-top: 6px;
  font-weight: 600;
  color: var(--text-primary);
}

.risk-body__label:first-child {
  margin-top: 0;
}

.risk-body__list {
  margin: 0;
  padding-left: 18px;
  display: flex;
  flex-direction: column;
  gap: 5px;
}

/* 差异化：每条 = 加粗关键词 + 短说明，说明用次级色退后一层；
   核心风险条目（is-danger）关键词用语义红 */
.risk-body__lead {
  font-weight: 600;
  color: var(--text-primary);
  margin-right: 6px;
}

.risk-body__rest {
  color: var(--text-secondary);
}

.risk-body__list li.is-danger .risk-body__lead {
  color: var(--danger);
}

.settings-page {
  max-width: 860px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.settings-card {
  padding: 20px 24px;
}

.settings-row {
  margin-top: 16px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.settings-row__line {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

/* ── 数据备份列表 ── */
.settings-backup-list {
  margin-top: 14px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.settings-backup-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 12px;
  border: 1px solid var(--border-soft);
  border-radius: var(--radius);
  background: var(--bg-soft);
  font-size: 12.5px;
}

.settings-backup-item > span:first-child {
  flex: 1;
  min-width: 0;
}

.settings-backup-actions {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
}

.settings-backup-download {
  color: var(--accent);
  font-size: 12.5px;
  text-decoration: none;
  padding: 4px 8px;
  border-radius: var(--radius);
}

.settings-backup-download:hover {
  background: var(--accent-soft);
}

/* 恢复两段确认态：红字警示 */
.settings-backup-armed {
  color: var(--danger) !important;
  font-weight: 700;
}

/* ── 绑定摘要区 ── */
.account-summary {
  margin-top: 18px;
  padding: 14px 16px;
  border: 1px solid var(--border-soft);
  border-radius: var(--radius-md, 10px);
  background: var(--bg-inset, var(--bg-card));
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.account-summary__warn {
  color: var(--danger, #e74c3c);
  font-size: 13px;
}

/* 账号区头部工具行：刷新余额靠右（与行内操作列同侧） */
.account-summary__head {
  display: flex;
  justify-content: flex-end;
}

/* ── 多账号列表行 ── */
.account-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.account-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 12px;
  border: 1px solid var(--border-soft);
  border-radius: var(--radius-md, 10px);
  background: var(--bg-card);
  flex-wrap: wrap;
}

.account-row.is-active {
  border-color: var(--accent);
}

.account-row__avatar {
  width: 40px;
  height: 40px;
  border-radius: 8px;
  object-fit: cover;
  border: 1px solid var(--border-soft);
  flex-shrink: 0;
}

.account-row__avatar--fallback {
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  color: var(--text-muted);
  background: var(--surface-inset, transparent);
}

/* ── 列对齐契约（本组样式是账户行跨行对齐的全部依赖，改动前先读注释）──
   行 = [头像 40px][身份 224px][统计 固定 6 列][操作 ≥160px]：
   前两段定宽 → 统计区起点跨行一致；统计区内部是固定像素列 →
   列位置只由列序号决定，与内容长短无关。任何一段改回「内容驱动宽度」
   （auto / 由内容撑的 min-width），错位立即复现（不同长度区名可差 ~70px）。 */
.account-row__id {
  flex: 0 0 224px;
  min-width: 0;
}

.account-row__name {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.account-row__name-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}

.account-row__friend {
  font-size: 12px;
  color: var(--text-muted);
  margin-top: 2px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.account-badge {
  font-size: 10.5px;
  font-weight: 500;
  padding: 1px 7px;
  border-radius: 999px;
  white-space: nowrap;
  flex-shrink: 0;
}

.account-badge--primary {
  color: var(--accent);
  border: 1px solid var(--accent);
  opacity: 0.9;
}

.account-badge--active {
  color: var(--success, #27ae60);
  border: 1px solid var(--success, #27ae60);
}

/* 同步状态三态：正常亮绿、失败亮红、未同步虚线收灰 */
.account-badge--sync-ok {
  color: var(--success, #27ae60);
  border: 1px solid var(--success, #27ae60);
  cursor: help;
}

.account-badge--sync-fail {
  color: var(--danger, #e74c3c);
  border: 1px solid var(--danger, #e74c3c);
  cursor: help;
}

.account-badge--sync-idle {
  color: var(--text-muted);
  border: 1px dashed var(--border-soft);
  cursor: help;
}

/* 统计区：固定模板列（grid 定宽），列位置与内容长短无关。
   列宽按最长内容定：余额 112（₴21,455.38）/ 币种 140（乌克兰格里夫纳，
   中文全名不截断）/ 地区 120（United States）/ 计数三列 60·68·60。
   总宽 620 与「统计区独占行」的可用宽（默认窗口约 724）留有窄窗余量。 */
.account-row__stats {
  display: grid;
  grid-template-columns: 112px 140px 120px 60px 68px 60px;
  align-items: start;
  gap: 12px;
  flex: 1 1 auto;
  min-width: 0;
}

.account-stat {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}

.account-stat__label {
  font-size: 11px;
  color: var(--text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.account-stat__value {
  font-size: 13px;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  /* 等宽数字：位数不同的金额/计数在定宽列内字宽一致（.hl-num 同款） */
  font-variant-numeric: tabular-nums;
}

.account-stat__value--balance {
  font-size: 15px;
  font-weight: 700;
  color: var(--accent);
}

/* 币种/地区超列内截断（英文长名 "Ukrainian Hryvnia" 超 116px）：
   完整名走组件根元素 title（悬停可见），绝不撑列 */
.account-stat :deep(.currency-flag),
.account-stat :deep(.region-flag) {
  min-width: 0;
}

.account-stat :deep(.currency-flag__name),
.account-stat :deep(.region-flag__name) {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 操作列定宽：活跃账号无「设为当前」——不定宽则两行按钮总宽不同，
   窄屏下会出现「一行统计换行、一行不换」的分叉（换行判定依赖剩余宽度） */
.account-row__actions {
  display: flex;
  gap: 4px;
  margin-left: auto;
  flex-shrink: 0;
  justify-content: flex-end;
  min-width: 160px;
}

.account-summary__meta {
  font-size: 12px;
  color: var(--text-muted);
}

/* ── Cookie 获取引导 ── */
.cookie-guide {
  margin-top: 4px;
  font-size: 13px;
  color: var(--text-muted);
}

.cookie-guide summary {
  cursor: pointer;
  user-select: none;
  color: var(--accent);
  width: fit-content;
}

.cookie-guide__steps {
  margin: 10px 0 0;
  padding-left: 20px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  color: var(--text-primary);
}

/* :deep() —— 第 2–4 步的词条值由 v-html 注入，拿不到 scoped 属性，
   普通后代选择器命中不了（同 bundles 的 calc-hint 说明）。 */
.cookie-guide__steps :deep(kbd) {
  padding: 1px 5px;
  border: 1px solid var(--border-soft);
  border-bottom-width: 2px;
  border-radius: 4px;
  font-size: 12px;
  background: var(--surface-inset, transparent);
}

.cookie-guide__steps :deep(code) {
  padding: 1px 4px;
  border-radius: 4px;
  background: var(--surface-inset, var(--bg-card));
  border: 1px solid var(--border-soft);
  font-size: 12px;
}

.cookie-guide__notes {
  margin-top: 8px;
  font-size: 12px;
}

/* ── 应用更新卡片 ── */
.settings-update-ready {
  margin-top: 14px;
  padding: 12px 14px;
  border: 1px solid var(--success, #27ae60);
  border-radius: 8px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

/* ── 通知区块 ── */
.notify-cat-label {
  font-weight: 600;
  color: var(--text-primary);
}
.notify-cats {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 22px;
}
.notify-times {
  align-items: center;
  gap: 8px;
}
.notify-times__sep {
  color: var(--text-dim);
}
.notify-status {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
  color: var(--text-secondary);
}
.notify-status__dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--rate-good);
  flex-shrink: 0;
}
.notify-status.is-bad .notify-status__dot {
  background: var(--danger);
}

.settings-update-ready__text {
  font-size: 13px;
  color: var(--text-primary);
}

.settings-update-notes {
  margin-top: 10px;
  padding: 10px 12px;
  border: 1px solid var(--border-soft);
  border-radius: 8px;
  background: var(--surface-inset, transparent);
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--text-secondary);
  white-space: pre-wrap;
  max-height: 240px;
  overflow-y: auto;
}

.settings-update-progress {
  margin-top: 12px;
}

.settings-update-progress__bar {
  margin-top: 6px;
  height: 4px;
  border-radius: 2px;
  background: var(--surface-inset, var(--bg-card));
  overflow: hidden;
}

.settings-update-progress__fill {
  height: 100%;
  border-radius: 2px;
  background: var(--accent);
  transition: width 0.3s ease;
}
</style>
