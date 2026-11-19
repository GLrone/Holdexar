<script setup lang="ts">
/** 局域网访问解锁页：密码解锁或 Steam 登录验证找回，成功后整页刷新进目标路由。 */
import { computed, onUnmounted, ref } from 'vue'
import { useI18n } from '@/locales'
import {
  recoveryCancel,
  recoveryComplete,
  recoveryStart,
  recoveryStatus,
  recoverySubmitCode,
  unlockAccess,
  ApiError,
  type RecoveryLoginState,
} from '@/api/client'
import { useThemeStore } from '@/stores/theme'
import { APP_NAME } from '@/appInfo'
import HlInput from '@/components/ui/HlInput.vue'
import HlButton from '@/components/ui/HlButton.vue'

defineOptions({ name: 'AccessUnlock' })

const { t } = useI18n()
const themeStore = useThemeStore()
const brandLogo = computed(() =>
  themeStore.isDark ? '/assets/logo_dark.ico' : '/assets/logo_light.ico',
)

/** Limit post-unlock redirects to safe same-origin relative paths and avoid looping back to /unlock. */
function safeNextPath(): string {
  const raw = new URLSearchParams(window.location.search).get('next') ?? ''
  if (!raw.startsWith('/') || raw.startsWith('//') || raw.startsWith('/\\')) return '/'
  if (raw === '/unlock' || raw.startsWith('/unlock?') || raw.startsWith('/unlock#')) return '/'
  for (let i = 0; i < raw.length; i++) {
    const code = raw.charCodeAt(i)
    if (code < 0x20 || code === 0x7f) return '/'
  }
  return raw
}

/* ── 密码解锁 ── */
const password = ref('')
const error = ref('')
const busy = ref(false)
const unlocked = ref(false)

async function submit() {
  if (busy.value || !password.value.trim()) return
  busy.value = true
  error.value = ''
  try {
    await unlockAccess(password.value.trim())
    unlocked.value = true
    window.location.assign(safeNextPath())
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : t('access.failed')
  } finally {
    busy.value = false
  }
}

/* ── Steam 找回：登录 → 状态轮询 → [验证码] → 完成 → 换会话 ── */
const view = ref<'password' | 'recover'>('password')
const recAccount = ref('')
const recPassword = ref('')
const recCode = ref('')
const recState = ref<RecoveryLoginState | null>(null)
const recError = ref('')
const recBusy = ref(false)
let pollTimer: number | undefined

function idleState(): RecoveryLoginState {
  return {
    state: 'idle',
    message: '',
    error: '',
    code_hint: '',
    code_available: false,
    started_at: '',
    updated_at: '',
  }
}

const recRunning = computed(
  () =>
    recState.value !== null &&
    ['signing', 'awaiting_code', 'awaiting_confirmation', 'finalizing'].includes(recState.value.state),
)
const recCodeHint = computed(() => {
  const hint = recState.value?.code_hint
  if (hint === 'email') return t('access.recoverCodeEmail')
  if (hint === 'totp') return t('access.recoverCodeTotp')
  return ''
})

function stopPolling() {
  if (pollTimer !== undefined) {
    window.clearInterval(pollTimer)
    pollTimer = undefined
  }
}

onUnmounted(stopPolling)

function switchView(target: 'password' | 'recover') {
  if (view.value === target) return
  stopPolling()
  if (view.value === 'recover') void recoveryCancel()
  view.value = target
  recError.value = ''
}

async function startRecovery() {
  if (recBusy.value || !recAccount.value.trim() || !recPassword.value) return
  recBusy.value = true
  recError.value = ''
  try {
    const result = await recoveryStart(recAccount.value.trim(), recPassword.value)
    recState.value = result.state ?? idleState()
    if (!result.ok) {
      recError.value = result.error || t('access.recoverStartFailed')
      return
    }
    recPassword.value = ''
    stopPolling()
    pollTimer = window.setInterval(pollOnce, 1200)
  } catch (e) {
    recError.value = e instanceof ApiError ? e.message : t('access.recoverStartFailed')
  } finally {
    recBusy.value = false
  }
}

async function pollOnce() {
  try {
    const st = await recoveryStatus()
    recState.value = st
    if (st.state === 'failed') {
      stopPolling()
      recError.value = st.error || t('access.failed')
    } else if (st.state === 'done') {
      stopPolling()
      await completeRecovery()
    }
  } catch {
    /* 单次轮询失败下次再试，总时限由后端登录状态机兜底 */
  }
}

async function submitCode() {
  if (recBusy.value || !recCode.value.trim()) return
  recBusy.value = true
  recError.value = ''
  try {
    const result = await recoverySubmitCode(recCode.value.trim())
    if (result.state) recState.value = result.state
    if (!result.ok) recError.value = result.error || t('access.failed')
    recCode.value = ''
  } catch (e) {
    recError.value = e instanceof ApiError ? e.message : t('access.failed')
  } finally {
    recBusy.value = false
  }
}

async function completeRecovery() {
  try {
    const result = await recoveryComplete()
    if (!result.ok) {
      recState.value = idleState()
      recError.value = result.error || t('access.failed')
      return
    }
    unlocked.value = true
    window.location.assign(safeNextPath())
  } catch (e) {
    recState.value = idleState()
    recError.value = e instanceof ApiError ? e.message : t('access.failed')
  }
}

async function cancelRecovery() {
  stopPolling()
  recBusy.value = true
  try {
    await recoveryCancel()
  } finally {
    recBusy.value = false
  }
  recState.value = idleState()
  recError.value = ''
  recCode.value = ''
}
</script>

<template>
  <div class="unlock-page">
    <div class="unlock-card">
      <div class="unlock-card__brand">
        <img class="unlock-card__logo" :src="brandLogo" :alt="APP_NAME" />
        <span class="unlock-card__name">{{ APP_NAME }}</span>
      </div>

      <!-- 密码解锁视图 -->
      <template v-if="view === 'password'">
        <h1 class="unlock-card__title">{{ t('access.title') }}</h1>
        <p class="unlock-card__hint">{{ t('access.hint') }}</p>
        <form class="unlock-card__form" @submit.prevent="submit">
          <HlInput
            v-model="password"
            name="password"
            type="password"
            show-password
            autocomplete="current-password"
            :aria-label="t('access.passwordPlaceholder')"
            :placeholder="t('access.passwordPlaceholder')"
            :disabled="busy || unlocked"
          />
          <HlButton
            variant="primary"
            size="lg"
            block
            type="submit"
            :disabled="busy || unlocked || !password.trim()"
          >
            {{ unlocked ? t('access.unlocked') : t('access.unlock') }}
          </HlButton>
        </form>
        <p v-if="error" class="unlock-card__error" role="alert">{{ error }}</p>
      </template>

      <!-- Steam 找回视图 -->
      <template v-else>
        <h1 class="unlock-card__title">{{ t('access.recoverTitle') }}</h1>
        <p class="unlock-card__hint">{{ t('access.recoverHint') }}</p>

        <form v-if="!recRunning" class="unlock-card__form" @submit.prevent="startRecovery">
          <HlInput
            v-model="recAccount"
            name="steam_account"
            autocomplete="username"
            autocorrect="off"
            autocapitalize="none"
            :spellcheck="false"
            :aria-label="t('access.recoverAccount')"
            :placeholder="t('access.recoverAccount')"
            :disabled="recBusy"
          />
          <HlInput
            v-model="recPassword"
            name="steam_password"
            type="password"
            show-password
            autocomplete="current-password"
            :aria-label="t('access.recoverPassword')"
            :placeholder="t('access.recoverPassword')"
            :disabled="recBusy"
          />
          <HlButton
            variant="primary"
            size="lg"
            block
            type="submit"
            :disabled="recBusy || !recAccount.trim() || !recPassword"
          >
            {{ recBusy ? t('access.unlocking') : t('access.recoverStart') }}
          </HlButton>
        </form>

        <div v-else class="unlock-card__form">
          <p class="unlock-card__state" aria-live="polite">
            {{ recState?.message || t('access.recoverWorking') }}
          </p>
          <form
            v-if="recState?.state === 'awaiting_code' && recState.code_available"
            class="unlock-card__form"
            @submit.prevent="submitCode"
          >
            <p v-if="recCodeHint" class="unlock-card__hint">{{ recCodeHint }}</p>
            <HlInput
              v-model="recCode"
              name="steam_code"
              autocomplete="one-time-code"
              autocorrect="off"
              autocapitalize="none"
              :spellcheck="false"
              :aria-label="t('access.recoverCodePlaceholder')"
              :placeholder="t('access.recoverCodePlaceholder')"
              :disabled="recBusy"
            />
            <HlButton variant="primary" block type="submit" :disabled="recBusy || !recCode.trim()">
              {{ t('access.recoverSubmitCode') }}
            </HlButton>
          </form>
          <HlButton type="button" variant="text" block :disabled="recBusy" @click="cancelRecovery">
            {{ t('access.recoverCancel') }}
          </HlButton>
        </div>

        <p v-if="recError" class="unlock-card__error" role="alert">{{ recError }}</p>
        <p v-if="unlocked" class="unlock-card__hint">{{ t('access.unlocked') }}</p>
      </template>

      <HlButton
        type="button"
        variant="text"
        size="sm"
        @click="switchView(view === 'password' ? 'recover' : 'password')"
      >
        {{ view === 'password' ? t('access.recover') : t('access.recoverBack') }}
      </HlButton>

      <div class="unlock-card__help">
        <p class="unlock-card__help-title">{{ t('access.forgot') }}</p>
        <p class="unlock-card__help-body">{{ t('access.forgotHint') }}</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 局域网解锁页（裸路由）：居中窄卡 + 移动基线（safe-area、触达目标、
   输入 16px 防 iOS 聚焦缩放——尺寸落在令牌行上，不引颜色字面量） */
.unlock-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: calc(env(safe-area-inset-top) + 1.5rem) 1.25rem
    calc(env(safe-area-inset-bottom) + 1.5rem);
}

.unlock-card {
  width: min(22rem, 100%);
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.unlock-card__brand {
  display: flex;
  align-items: center;
  gap: 0.625rem;
}

.unlock-card__logo {
  width: 2.25rem;
  height: 2.25rem;
  border-radius: 0.5rem;
  flex-shrink: 0;
}

.unlock-card__name {
  font-size: 1.125rem;
  font-weight: 700;
  letter-spacing: 0.02em;
}

.unlock-card__title {
  font-size: 1.25rem;
  font-weight: 600;
  margin: 0;
}

.unlock-card__hint {
  font-size: 0.875rem;
  opacity: 0.72;
  margin: 0;
}

.unlock-card__form {
  display: flex;
  flex-direction: column;
  gap: 0.875rem;
}

.unlock-card__form :deep(input) {
  font-size: 1rem;
}

.unlock-card__state {
  font-size: 0.875rem;
  margin: 0;
  opacity: 0.85;
}

.unlock-card__error {
  font-size: 0.875rem;
  margin: 0;
  color: var(--danger);
}

.unlock-card__help {
  font-size: 0.8125rem;
  opacity: 0.72;
  line-height: 1.5;
}

.unlock-card__help-title {
  margin: 0 0 0.25rem;
  font-weight: 600;
}

.unlock-card__help-body {
  margin: 0;
}
</style>
