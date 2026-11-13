<script setup lang="ts">
/** 局域网访问解锁页：访问密码换会话 Cookie，成功后整页刷新进应用。 */
import { ref } from 'vue'
import { useI18n } from '@/locales'
import { unlockAccess, ApiError } from '@/api/client'
import HlInput from '@/components/ui/HlInput.vue'
import HlButton from '@/components/ui/HlButton.vue'

defineOptions({ name: 'AccessUnlock' })

const { t } = useI18n()
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
    // 整页刷新：会话 Cookie 就位后，壳与各 store 的启动调用重新来过
    window.location.assign('/')
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : t('access.failed')
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="unlock-page">
    <div class="unlock-card">
      <h1 class="unlock-card__title">{{ t('access.title') }}</h1>
      <p class="unlock-card__hint">{{ t('access.hint') }}</p>
      <form class="unlock-card__form" @submit.prevent="submit">
        <HlInput
          v-model="password"
          type="password"
          show-password
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

.unlock-card__error {
  font-size: 0.875rem;
  margin: 0;
  color: var(--danger);
}
</style>
