<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { currencyName } from '@/api/currencies'
import {
  accountLoginApi,
  notificationsApi,
  settingsApi,
  pilotApi,
  systemApi,
  type LoginSessionState,
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
import { walletSyncOk, walletSyncedAt } from '@/lib/walletSync'
import CurrencyFlag from '@/components/CurrencyFlag.vue'
import RegionFlag from '@/components/RegionFlag.vue'
import {
  HlButton, HlCheckbox, HlDialog, HlIcon, HlImg, HlInput, HlSelect, HlSkeleton, HlSwitch, message,
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

/* 工具箱次级入口：工具箱已移出一级导航，这里是其常驻入口 */
const router = useRouter()
function openToolbox() {
  void router.push('/toolbox')
}

const steamId = ref('')
const apiKeyInput = ref('')
const apiKeyMasked = ref('')
const hasApiKey = ref(false)

/* ── 领航员（pilot）：AI 解读服务配置（key 密文落库，留空不改） ── */
const pilotEnabled = ref(false)
const pilotProtocol = ref('openai')
const pilotBaseUrl = ref('')
const pilotModel = ref('')
const pilotApiKeyInput = ref('')
const pilotHasApiKey = ref(false)
const pilotCapText = ref('500000')
const pilotUsage = ref(0)
const pilotModels = ref<string[]>([])
const pilotDetecting = ref(false)
/* 协议选项存 value 不存文案（切语言跟随）；地址占位随协议变化 */
const pilotProtocolOptions = computed(() => [
  { value: 'openai', label: t('pilot.proto.openai') },
  { value: 'anthropic', label: t('pilot.proto.anthropic') },
  { value: 'gemini', label: t('pilot.proto.gemini') },
  { value: 'ollama', label: t('pilot.proto.ollama') },
])
const pilotBaseUrlPlaceholder = computed(() =>
  t(`pilot.proto.base_${pilotProtocol.value}` as MessageKey))
const pilotSaving = ref(false)

async function detectPilot() {
  pilotDetecting.value = true
  try {
    const key = pilotApiKeyInput.value.trim()
    const r = await pilotApi.detect({
      base_url: pilotBaseUrl.value.trim(),
      ...(key ? { api_key: key } : {}),
    })
    pilotProtocol.value = r.protocol
    pilotModels.value = r.models || []
    if (!pilotModel.value && r.models?.length) pilotModel.value = r.models[0]
    if (r.key_valid === false) {
      message.error(t('pilot.detect.keybad'))
    } else if (r.models?.length) {
      message.success(t('pilot.detect.ok', { vendor: r.vendor || t('pilot.title'), n: r.models.length }))
    } else {
      message.info(t('pilot.detect.nomodels', { vendor: r.vendor || t('pilot.title') }))
    }
  } catch {
    message.error(t('pilot.detect.unreachable'))
  } finally {
    pilotDetecting.value = false
  }
}

async function loadPilot() {
  try {
    const cfg = await pilotApi.getConfig()
    pilotEnabled.value = cfg.enabled
    pilotProtocol.value = cfg.protocol || 'openai'
    pilotBaseUrl.value = cfg.base_url
    pilotModel.value = cfg.model
    pilotHasApiKey.value = cfg.has_api_key
    pilotCapText.value = String(cfg.monthly_cap)
    pilotUsage.value = cfg.usage_month
  } catch {
    /* 领航员配置拉不到不影响设置页其余部分 */
  }
}

async function savePilot() {
  pilotSaving.value = true
  try {
    const key = pilotApiKeyInput.value.trim()
    const cfg = await pilotApi.updateConfig({
      protocol: pilotProtocol.value,
      enabled: pilotEnabled.value,
      base_url: pilotBaseUrl.value.trim(),
      model: pilotModel.value.trim(),
      ...(key ? { api_key: key } : {}),
      monthly_cap: Number(pilotCapText.value) || 0,
    })
    pilotHasApiKey.value = cfg.has_api_key
    pilotUsage.value = cfg.usage_month
    pilotApiKeyInput.value = ''
    message.success(t('pilot.settings.saved'))
  } catch (e) {
    errorMsg.value = e instanceof Error ? e.message : String(e)
  } finally {
    pilotSaving.value = false
  }
}

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
/** 当前账号登录态已过期（访问令牌到期）；是否还需用户动手看 session_has_refresh */
const sessionExpired = computed(() => accountStore.status?.session_expired ?? false)
/** 留有续期凭据：过期后系统会自行续期，用户不必立刻重新登录 */
const sessionHasRefresh = computed(() => accountStore.status?.session_has_refresh ?? false)

/** active 账号的钱包（顶层 wallet 与 active 一致） */
const wallet = computed(() => accountStore.status?.wallet ?? null)
/** 账户摘要的同步时刻：缓存窗内取上次成功时刻，窗外取最近尝试时刻 */
const walletMetaTime = computed(() => (wallet.value ? walletSyncedAt(wallet.value) : null))

/* 每账号同步状态：登录过期优先于同步结果（过期的账号余额本就抓不到）；
   同步成功走显示缓存窗：上次成功获取余额（wallet.ok_at）在 10 分钟内即按
   成功呈现（余额本身是窗内的真实数据），窗外的失败才如实显示；
   无钱包快照 = 尚未同步。 */
type SyncTone = 'ok' | 'fail' | 'idle'
function syncStateOf(acc: SteamAccountItem): { tone: SyncTone; label: string; tip: string } {
  if (acc.session_expired) {
    // 有续期凭据 = 系统自己在续，不需要用户动作（给中性态 + 兜底出路）
    return acc.session_has_refresh
      ? {
          tone: 'idle',
          label: t('settings.steam.syncRenewing'),
          tip: t('settings.steam.syncTipRenewing'),
        }
      : {
          tone: 'fail',
          label: t('settings.steam.syncExpired'),
          tip: t('settings.steam.syncTipExpired'),
        }
  }
  if (!acc.wallet) {
    return { tone: 'idle', label: t('settings.steam.syncIdle'), tip: t('settings.steam.syncTipIdle') }
  }
  const time = walletSyncedAt(acc.wallet)
  const timeText = time ? time.replace('T', ' ').slice(0, 19) : ''
  const error = acc.wallet_error || acc.wallet.error || ''
  if (error && !walletSyncOk(acc.wallet)) {
    return {
      tone: 'fail',
      label: t('settings.steam.syncFail'),
      tip: timeText ? t('settings.steam.syncTipFail', { time: timeText, error }) : t('settings.steam.syncFail'),
    }
  }
  return {
    tone: 'ok',
    label: t('settings.steam.syncOk'),
    tip: timeText ? t('settings.steam.syncTipOk', { time: timeText }) : t('settings.steam.syncOk'),
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
const backupAutoOn = computed(() => settingsStore.backupAuto !== false)
const updatePrefsToggling = ref(false)
const backupToggling = ref(false)

async function toggleBackupAuto(on: boolean) {
  if (backupToggling.value) return
  backupToggling.value = true
  try {
    const ok = await settingsStore.setBackupAuto(on)
    if (ok) message.success(t(on ? 'settings.backup.autoOn' : 'settings.backup.autoOff'))
    else message.error(t('settings.update.switchFailed'))
  } finally {
    backupToggling.value = false
  }
}

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
  /* 六个数据来源互不依赖：主表单数据先发出，其余并行跟进，骨架屏只等主数据；
     账户 / 备份列表 / 更新状态各自吞错，到数即填对应卡片，不阻塞页面 */
  const main = settingsApi.get()
  void accountStore.load()
  void loadBackups()
  void loadUpdateInfo()
  try {
    const data: SettingsPayload = await main
    steamId.value = data.account.steam_id
    apiKeyMasked.value = data.account.steam_api_key
    hasApiKey.value = data.account.has_api_key
  } catch (e) {
    errorMsg.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function loadUpdateInfo() {
  /* 版本信息 + 与后端对齐更新状态（暂存/进行中的下载；失败静默不阻塞设置页） */
  try {
    const info = await systemApi.info()
    appVersion.value = info.version
    repoSlug.value = info.repo
    await updaterStore.sync()
  } catch {
    /* 更新状态拉不到不影响设置页 */
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
  } else if (walletSyncOk(status?.wallet)) {
    message.success(t('settings.toast.bindSuccess', { balance: status.wallet.balance_display }))
  } else if (status?.sync_error) {
    message.warning(t('settings.toast.bindSyncFailed', { error: status.sync_error }))
  } else {
    message.success(t('settings.toast.cookieSaved'))
  }
  // 没有续期凭据 = 这份登录态约一天后到期（登录 Steam 时未勾选「记住我」）
  if (status?.has_cookie && status.session_has_refresh === false) {
    message.warning(t('settings.toast.noRefreshToken'))
  }
}

/* ── 绑定风险弹窗 ────────────────────────────────────────────
   每次绑定动作（账号密码登录 / 手动粘贴）都会先弹；标红同意勾选框
   勾选后确定键才可用，每次打开重新勾选、不做停留时长强制。确定后才
   继续原流程——'login' 走应用内账号密码登录，'manual' 提交已粘贴的
   Cookie。 */
type RiskNextAction = 'login' | 'manual'

/* 弹窗正文条目：每条 = 加粗关键词(lead) + 短说明(rest)，扫加粗词即可抓住重点；
   首条（凭据加密保存）是核心风险，danger 色强调。常量只存词条 key 不存译文
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
  { lead: 'settings.risk.item5Lead', rest: 'settings.risk.item5Rest' },
]
const LEAK_ITEMS: RiskItem[] = [
  { lead: 'settings.risk.leak1Lead', rest: 'settings.risk.leak1Rest' },
  { lead: 'settings.risk.leak2Lead', rest: 'settings.risk.leak2Rest' },
  { lead: 'settings.risk.leak3Lead', rest: 'settings.risk.leak3Rest' },
  { lead: 'settings.risk.leak4Lead', rest: 'settings.risk.leak4Rest' },
]
const riskDialogOpen = ref(false)
const riskNextAction = ref<RiskNextAction>('manual')
// 标红同意勾选：未勾选时确定键禁用；每次打开弹窗重置，逐次确认
const riskConsent = ref(false)

function openRiskDialog(action: RiskNextAction) {
  riskNextAction.value = action
  riskConsent.value = false
  riskDialogOpen.value = true
}

function riskConfirm() {
  if (!riskConsent.value) return
  riskDialogOpen.value = false
  if (riskNextAction.value === 'login') void startPasswordLogin()
  else void bindCookie()
}

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

/* ── 应用内账号密码登录：后端直调 Steam 认证 API，状态机经 /account/login/*
   轮询驱动；登录成功即由后端完成绑定（钱包试抓 + 全量数据后台拉取）。
   会话在后端存续，切页回来凭 status 快照恢复界面。 ── */
const loginAccount = ref('')
const loginPassword = ref('')
const loginState = ref<LoginSessionState | null>(null)
const loginCodeInput = ref('')
const loginCodeError = ref('')
const loginCodeAccepted = ref(false)
/* 等待确认态下的输码切换：手机验证器账号默认走「手机上确认」，此开关
   对应登录页「改为输入代码」的备选路径；回到等待/登录结束即复位 */
const loginCodeMode = ref(false)
let loginTimer: number | null = null

const LOGIN_BUSY_STATES = new Set(['signing', 'awaiting_code', 'awaiting_confirmation', 'finalizing'])
const loginActive = computed(() => !!loginState.value && LOGIN_BUSY_STATES.has(loginState.value.state))

function stopLoginPolling() {
  if (loginTimer !== null) {
    window.clearInterval(loginTimer)
    loginTimer = null
  }
}

function startLoginPolling() {
  stopLoginPolling()
  loginTimer = window.setInterval(void refreshLoginState, 1000)
}

async function refreshLoginState() {
  try {
    const st = await accountLoginApi.status()
    loginState.value = st
    if (st.state === 'done') {
      stopLoginPolling()
      loginCodeMode.value = false
      message.success(t('settings.steam.loginDone'))
      loginAccount.value = ''
      loginCodeInput.value = ''
      loginCodeError.value = ''
      loginCodeAccepted.value = false
      await accountStore.load()
      window.setTimeout(() => {
        if (loginState.value?.state === 'done') loginState.value = null
      }, 4000)
    } else if (st.state === 'idle') {
      stopLoginPolling()
      loginCodeMode.value = false
      loginState.value = null
    }
  } catch {
    /* 轮询单次网络抖动不打断登录，下一轮重试 */
  }
}

async function startPasswordLogin() {
  if (!loginAccount.value.trim() || !loginPassword.value) {
    message.warning(t('settings.steam.loginAccountPlaceholder'))
    return
  }
  loginState.value = { ...emptyLoginState, state: 'signing' }
  loginCodeMode.value = false
  try {
    const res = await accountLoginApi.start(loginAccount.value.trim(), loginPassword.value)
    loginPassword.value = ''
    loginState.value = res.state
    if (res.busy) {
      message.warning(t('settings.steam.loginBusy'))
      await refreshLoginState()
    } else if (res.ok) {
      startLoginPolling()
    } else if (res.state.state === 'failed') {
      stopLoginPolling()
    }
  } catch (e) {
    loginState.value = null
    message.error(e instanceof Error ? e.message : String(e))
  }
}

const emptyLoginState: LoginSessionState = {
  state: 'idle',
  message: '',
  error: '',
  code_hint: '',
  started_at: '',
  updated_at: '',
}

async function submitLoginCode() {
  if (!loginCodeInput.value.trim()) return
  loginCodeError.value = ''
  try {
    const res = await accountLoginApi.code(loginCodeInput.value)
    loginState.value = res.state
    if (res.ok) {
      loginCodeInput.value = ''
      loginCodeAccepted.value = true
      loginCodeMode.value = false
    } else {
      loginCodeError.value = res.error || ''
    }
  } catch (e) {
    loginCodeError.value = e instanceof Error ? e.message : String(e)
  }
}

async function cancelLoginFlow() {
  stopLoginPolling()
  try {
    await accountLoginApi.cancel()
  } catch {
    /* 取消失败无需阻断界面复位 */
  }
  loginState.value = null
  loginCodeInput.value = ''
  loginCodeError.value = ''
  loginCodeAccepted.value = false
  loginCodeMode.value = false
}

const loginStateTip = computed(() => {
  const st = loginState.value
  if (!st) return ''
  if (st.state === 'signing') return t('settings.steam.loginSigning')
  if (st.state === 'awaiting_confirmation') {
    return loginCodeAccepted.value
      ? t('settings.steam.loginConfirmWaitCode')
      : t('settings.steam.loginConfirmWait')
  }
  if (st.state === 'finalizing') return t('settings.steam.loginFinalizing')
  if (st.state === 'done') return t('settings.steam.loginDone')
  return ''
})

/* 输码块的呈现条件：邮箱/令牌码形态直接呈现；确认形态账号经「改为输入
   验证码」切换后复用同一块（对应登录页「改为输入代码」的备选路径） */
const showLoginCodeEntry = computed(() => {
  const st = loginState.value
  if (!st) return false
  if (st.state === 'awaiting_code') return true
  return st.state === 'awaiting_confirmation' && loginCodeMode.value
})

const loginCodeHint = computed(() =>
  loginState.value?.code_hint === 'email'
    ? t('settings.steam.loginCodeHintEmail')
    : t('settings.steam.loginCodeHintTotp'),
)

async function resyncWallet() {
  cookieSaving.value = true
  message.loading(t('settings.toast.fetching'))
  try {
    await accountStore.sync()
    if (walletSyncOk(accountStore.status?.wallet)) {
      message.success(t('settings.toast.walletRefreshed'))
    } else if (accountStore.status?.session_expired) {
      if (accountStore.status?.session_has_refresh) message.error(t('settings.steam.syncTipRenewing'))
      else message.error(t('settings.steam.syncTipExpired'))
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
  void loadPilot()
  /* 登录会话在后端存续：进页先对状态快照，进行中就恢复状态卡并续上轮询 */
  void refreshLoginState().then(() => {
    if (loginActive.value) startLoginPolling()
  })
})

onUnmounted(stopLoginPolling)
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

        <!-- 应用内账号密码登录：后端直调 Steam 认证 API，二次验证与应用内完成 -->
        <div class="settings-row login-block">
          <div class="settings-row__line">
            <HlInput
              v-model="loginAccount"
              :placeholder="t('settings.steam.loginAccountPlaceholder')"
              :disabled="loginActive"
              class="login-block__input"
            />
            <HlInput
              v-model="loginPassword"
              type="password"
              :placeholder="t('settings.steam.loginPassword')"
              :disabled="loginActive"
              class="login-block__input"
              @keydown.enter="openRiskDialog('login')"
            />
            <HlButton
              art="outline"
              tone="blue"
              size="sm"
              :disabled="loginActive || cookieSaving"
              :loading="loginActive"
              @click="openRiskDialog('login')"
            >
              <HlIcon v-if="!loginActive" name="zap" />
              {{ t('settings.steam.autoFetch') }}
            </HlButton>
          </div>

          <!-- 登录状态卡：进行中 / 失败各有形态，成功后自动收起 -->
          <div v-if="loginState && loginState.state !== 'idle'" class="login-state" :class="`is-${loginState.state}`">
            <template v-if="showLoginCodeEntry">
              <div class="login-state__title">{{ t('settings.steam.loginCodeTitle') }}</div>
              <div class="login-state__hint">{{ loginCodeHint }}</div>
              <div class="settings-row__line">
                <HlInput
                  v-model="loginCodeInput"
                  :placeholder="t('settings.steam.loginCodePlaceholder')"
                  class="login-block__code"
                  @keydown.enter="submitLoginCode"
                />
                <HlButton art="outline" tone="green" size="sm" @click="submitLoginCode">
                  {{ t('settings.steam.loginCodeSubmit') }}
                </HlButton>
                <HlButton
                  v-if="loginState.state === 'awaiting_confirmation'"
                  variant="text"
                  size="sm"
                  @click="loginCodeMode = false"
                >
                  {{ t('settings.steam.loginBackToConfirm') }}
                </HlButton>
                <HlButton variant="text" size="sm" @click="cancelLoginFlow">
                  {{ t('settings.steam.loginCancel') }}
                </HlButton>
              </div>
              <div v-if="loginCodeError" class="login-state__error">{{ loginCodeError }}</div>
            </template>

            <template v-else-if="loginState.state === 'failed'">
              <div class="login-state__error">{{ loginState.error }}</div>
              <HlButton variant="text" size="sm" @click="cancelLoginFlow">
                {{ t('settings.steam.loginFailedRetry') }}
              </HlButton>
            </template>

            <template v-else>
              <div class="login-state__hint">
                <span class="login-state__spin" aria-hidden="true"></span>
                {{ loginStateTip }}
              </div>
              <div class="settings-row__line">
                <HlButton
                  v-if="loginState.state === 'awaiting_confirmation' && loginState.code_available && !loginCodeAccepted"
                  variant="text"
                  size="sm"
                  @click="loginCodeMode = true"
                >
                  {{ t('settings.steam.loginSwitchCode') }}
                </HlButton>
                <HlButton variant="text" size="sm" @click="cancelLoginFlow">
                  {{ t('settings.steam.loginCancel') }}
                </HlButton>
              </div>
            </template>
          </div>
        </div>

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
          <div v-if="sessionExpired" class="account-summary__warn">
            {{ sessionHasRefresh ? t('settings.steam.sessionRenewingWarn') : t('settings.steam.sessionExpiredWarn') }}
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

          <div v-if="walletMetaTime" class="account-summary__meta">
            {{
              t('settings.steam.syncMeta', {
                time: walletMetaTime.replace('T', ' ').slice(0, 19),
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

      <!-- 领航员（AI 解读服务配置） -->
      <div class="card settings-card" data-section="pilot.settings.title">
        <div class="section-title">{{ t('pilot.settings.title') }}</div>
        <div class="section-desc">{{ t('pilot.settings.desc') }}</div>

        <div class="settings-row">
          <div class="settings-row__line">
            <HlSwitch v-model="pilotEnabled" accent :label="t('pilot.settings.enabled')" />
          </div>
          <div class="settings-row__line">
            <HlSelect
              v-model="pilotProtocol"
              :options="pilotProtocolOptions"
              style="max-width: 420px"
            />
          </div>
          <div class="settings-row__line">
            <HlInput
              v-model="pilotBaseUrl"
              :placeholder="pilotBaseUrlPlaceholder"
              style="max-width: 340px"
            />
            <HlButton
              size="sm"
              :disabled="!pilotBaseUrl.trim() || pilotDetecting"
              :loading="pilotDetecting"
              @click="detectPilot"
            >
              {{ t('pilot.detect.button') }}
            </HlButton>
          </div>
          <div class="settings-row__line">
            <HlSelect
              v-if="pilotModels.length"
              v-model="pilotModel"
              :options="pilotModels.map((m) => ({ value: m, label: m }))"
              style="max-width: 420px"
            />
            <HlInput
              v-else
              v-model="pilotModel"
              :placeholder="t('pilot.settings.model')"
              style="max-width: 420px"
            />
          </div>
          <div class="settings-row__line">
            <HlInput
              v-model="pilotApiKeyInput"
              show-password
              :placeholder="
                pilotHasApiKey
                  ? t('pilot.settings.api_key_hint')
                  : t('pilot.settings.api_key')
              "
              style="max-width: 420px"
            />
          </div>
          <div class="settings-row__line">
            <HlInput
              v-model="pilotCapText"
              :placeholder="t('pilot.settings.monthly_cap')"
              style="max-width: 420px"
            />
            <span class="section-desc" style="display: inline; margin-left: 8px">
              {{ t('pilot.settings.usage', { tokens: pilotUsage }) }}
            </span>
          </div>
        </div>

        <div class="settings-row">
          <div class="settings-row__line">
            <HlButton
              art="outline"
              tone="blue"
              size="sm"
              :disabled="pilotSaving"
              :loading="pilotSaving"
              @click="savePilot"
            >
              <HlIcon name="check" />
              {{ t('pilot.settings.save') }}
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
            <HlSwitch
              :model-value="backupAutoOn"
              accent
              :disabled="backupToggling"
              :label="t('settings.backup.autoLabel')"
              :title="t('settings.backup.autoHint')"
              @update:model-value="toggleBackupAuto"
            />
          </div>
          <div class="section-desc">{{ t('settings.backup.autoHint') }}</div>
        </div>

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

      <!-- 工具箱次级入口：工具箱不进一级导航，页内能力（账单摘要 / CDK 激活）
           从这里到达；路由与页面保留 -->
      <div class="card settings-card" data-section="settings.section.toolbox">
        <div class="section-title">{{ t('settings.section.toolbox') }}</div>
        <div class="section-desc">{{ t('settings.toolbox.desc') }}</div>
        <HlButton size="sm" @click="openToolbox">{{ t('settings.toolbox.open') }}</HlButton>
      </div>
    </template>

    <!-- 绑定风险弹窗：每次绑定动作（手动/自动）都会弹出，标红同意勾选后
         确定键才可用；确定后才继续——'auto' 打开 Steam 登录子窗口，'manual'
         提交已粘贴的 Cookie -->
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
        <HlCheckbox v-model="riskConsent" class="risk-consent">
          <span class="risk-consent__text">{{ t('settings.risk.consent') }}</span>
        </HlCheckbox>
      </div>
      <template #footer>
        <HlButton variant="text" size="sm" @click="riskDialogOpen = false">
          {{ t('common.cancel') }}
        </HlButton>
        <HlButton art="outline" tone="green" size="sm" :disabled="!riskConsent" @click="riskConfirm">
          <HlIcon v-if="riskConsent" name="check" />
          {{ t('settings.risk.confirm') }}
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

/* 标红同意勾选：勾选后确定键才可用（每次打开重新勾选） */
.risk-consent {
  margin-top: 12px;
}

.risk-consent__text {
  color: var(--danger);
  font-weight: 600;
  line-height: 1.6;
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

/* ── 应用内账号密码登录 ── */
.login-block {
  gap: 8px;
}

.login-block__input {
  max-width: 200px;
}

.login-block__code {
  max-width: 140px;
}

/* 状态卡：左侧色条区分语义（蓝=进行中，绿=成功，红=失败） */
.login-state {
  border: 1px solid var(--border-soft, rgba(255, 255, 255, 0.12));
  border-left: 3px solid var(--primary, #4a90d9);
  border-radius: 8px;
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  align-items: flex-start;
  font-size: 13px;
}

.login-state.is-done {
  border-left-color: var(--success, #4caf7d);
}

.login-state.is-failed {
  border-left-color: var(--danger, #e74c3c);
}

.login-state__title {
  font-weight: 700;
}

.login-state__hint {
  color: var(--text-secondary, inherit);
  display: flex;
  align-items: center;
  gap: 8px;
}

.login-state__error {
  color: var(--danger, #e74c3c);
}

/* 等待指示点：三拍呼吸，reduced-motion 下静止（总闸归零同样瞬时） */
.login-state__spin {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--primary, #4a90d9);
  animation: login-pulse calc(var(--duration-3, 1.2s) * var(--motion-scale, 1)) infinite;
}

@keyframes login-pulse {
  0%,
  100% {
    opacity: 0.35;
  }
  50% {
    opacity: 1;
  }
}

@media (prefers-reduced-motion: reduce) {
  .login-state__spin {
    animation: none;
  }
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
