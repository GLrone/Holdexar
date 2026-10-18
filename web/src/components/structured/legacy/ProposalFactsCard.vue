<script setup lang="ts">
import { computed, ref } from 'vue'
import type { PilotProposalFacts } from '@/api/client'
import { useI18n, type MessageKey } from '@/locales'
import { pilotCoverUrl } from '@/lib/pilotView'
import HlButton from '@/components/ui/HlButton.vue'
import HlImg from '@/components/ui/HlImg.vue'
import { fen } from '../helpers'

const props = defineProps<{
  data: PilotProposalFacts
  busyPid?: string | null
}>()

const emit = defineEmits<{
  (e: 'proposal', payload: { pid: string; approve: boolean }): void
}>()

const { t } = useI18n()
const c = computed(() => props.data)

function proposalTitle(p: PilotProposalFacts): string {
  const count = p.items.length
  if (p.action === 'delete') return t('pilot.proposal.delete', { count })
  if (p.action === 'add_follow') return t('pilot.proposal.follow', { count })
  if (p.args?.target_type === 'price') {
    const yuan = p.args.target_value_yuan
    return t('pilot.proposal.alertPrice', { count, price: typeof yuan === 'number' ? fen(yuan * 100) : '—' })
  }
  return t('pilot.proposal.alertLow', { count })
}

function proposalResult(p: PilotProposalFacts): string {
  if (p.state === 'rejected') return t('pilot.proposal.rejected')
  if (p.state === 'withdrawn') return t('pilot.proposal.withdrawn')
  if (p.state === 'dismissed') return t('pilot.proposal.dismissed')
  const done = p.done ?? p.items.length
  const text = t('pilot.proposal.done', { done })
  return p.failedCount ? `${text} · ${t('pilot.proposal.failed', { count: p.failedCount })}` : text
}

const imeComposing = ref(false)
const imeEnded = ref(false)

function onKeydown(e: KeyboardEvent) {
  if (c.value.state !== 'pending' || props.busyPid === c.value.pid) return
  if (e.key !== 'Enter' && e.key !== 'Escape') return
  e.preventDefault()
  if (imeComposing.value || imeEnded.value || e.isComposing) return
  emit('proposal', { pid: c.value.pid, approve: e.key === 'Enter' })
}
</script>

<template>
  <div class="hsui-proposal-card">
    <div
      class="pcard-proposal"
      :tabindex="c.state === 'pending' ? 0 : undefined"
      @keydown="onKeydown"
      @compositionstart.capture="imeComposing = true"
      @compositionend.capture="imeComposing = false; imeEnded = true"
      @keyup.capture="imeEnded = false"
    >
      <div class="pcard-proposal__head">
        <div class="pcard-proposal__shield">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
          </svg>
        </div>
        <div class="pcard-proposal__titles">
          <span class="pcard-proposal__main-title">{{ proposalTitle(c) }}</span>
          <span v-if="c.state === 'pending'" class="pcard-proposal__desc">{{ t('pilot.proposal.hint') }}</span>
        </div>
      </div>

      <div v-if="c.action === 'delete'" class="pcard-del-list">
        <div v-for="it in c.items.slice(0, 8)" :key="it.key ?? it.appid" class="pcard-del-row">
          <HlImg
            v-if="it.appid"
            :src="pilotCoverUrl(it.appid)"
            :alt="it.name"
            loading="lazy"
            class="pcard-del-row__cover"
          >
            <template #fallback>
              <span>{{ (it.name || `AppID ${it.appid}`).slice(0, 2) }}</span>
            </template>
          </HlImg>
          <div class="pcard-del-row__main">
            <span class="pcard-del-row__name">{{ it.name }}</span>
            <span v-if="it.reason" class="pcard-del-row__reason">{{ t(`pilot.del.reason.${it.reason}` as MessageKey) }}</span>
          </div>
        </div>
        <span v-if="c.items.length > 8" class="pcard-del-more-pill">+{{ c.items.length - 8 }}</span>
      </div>

      <div v-else class="pcard-item-chips">
        <span
          v-for="it in c.items.slice(0, 8)"
          :key="it.key ?? it.appid ?? it.name"
          class="pcard-item-chip"
          :class="{ 'has-cover': Boolean(it.appid) }"
        >
          <HlImg
            v-if="it.appid"
            :src="pilotCoverUrl(it.appid)"
            :alt="it.name"
            loading="lazy"
            class="pcard-item-chip__cover"
          >
            <template #fallback>
              <span class="pcard-item-chip__cover-fallback">{{ (it.name || `AppID ${it.appid}`).slice(0, 1) }}</span>
            </template>
          </HlImg>
          <span class="pcard-item-chip__name" :title="it.name">{{ it.name }}</span>
        </span>
        <span v-if="c.items.length > 8" class="pcard-item-chip pcard-item-chip--more">+{{ c.items.length - 8 }}</span>
      </div>

      <div v-if="c.state === 'pending'" class="pcard-proposal__actions">
        <HlButton
          variant="primary"
          size="sm"
          :disabled="busyPid === c.pid"
          @click="emit('proposal', { pid: c.pid, approve: true })"
        >
          {{ t('pilot.proposal.confirm') }}
        </HlButton>
        <HlButton
          variant="text"
          size="sm"
          :disabled="busyPid === c.pid"
          @click="emit('proposal', { pid: c.pid, approve: false })"
        >
          {{ t('pilot.proposal.cancel') }}
        </HlButton>
      </div>

      <div v-else class="pcard-receipt">
        <div class="pcard-receipt__dot"></div>
        <span class="pcard-receipt__text">{{ proposalResult(c) }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.hsui-proposal-card {
  width: 100%;
}

.pcard-proposal {
  display: flex;
  flex-direction: column;
  gap: 10px;
  outline: none;
}

.pcard-proposal__head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.pcard-proposal__shield {
  width: 24px;
  height: 24px;
  color: var(--accent, #66c0f4);
  flex-shrink: 0;
}

.pcard-proposal__titles {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.pcard-proposal__main-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary, #d9e3ec);
}

.pcard-proposal__desc {
  font-size: 11px;
  color: var(--text-dim, #5a7080);
}

.pcard-del-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.pcard-del-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 6px;
  background: var(--surface-inset, rgba(0, 0, 0, 0.2));
  border-radius: 4px;
}

.pcard-del-row__cover {
  width: 36px;
  height: 20px;
  border-radius: 2px;
  object-fit: cover;
  flex-shrink: 0;
}

.pcard-del-row__main {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
}

.pcard-del-row__name {
  font-size: 12px;
  color: var(--text-secondary, #c7d5e0);
}

.pcard-del-row__reason {
  font-size: 11px;
  color: var(--danger, #e74c3c);
}

.pcard-del-more-pill {
  font-size: 11px;
  color: var(--text-dim, #5a7080);
  text-align: center;
}

.pcard-item-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.pcard-item-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 6px;
  background: var(--surface-inset, rgba(0, 0, 0, 0.2));
  border-radius: 4px;
  font-size: 11px;
  color: var(--text-secondary, #c7d5e0);
}

.pcard-item-chip__cover {
  width: 18px;
  height: 18px;
  border-radius: 2px;
  object-fit: cover;
}

.pcard-proposal__actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 4px;
}

.pcard-receipt {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  background: var(--surface-inset, rgba(0, 0, 0, 0.15));
  border-radius: 4px;
  font-size: 12px;
}

.pcard-receipt__dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--success, #a4d007);
}

.pcard-receipt__text {
  color: var(--text-secondary, #c7d5e0);
}
</style>
