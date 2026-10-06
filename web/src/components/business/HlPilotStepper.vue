<script setup lang="ts">
import { computed } from 'vue'
import type { PilotStepperFacts } from '@/api/client'
import { useI18n } from '@/locales'

const props = defineProps<{
  card: PilotStepperFacts
}>()

const { t } = useI18n()

const steps = computed(() => props.card.steps || [])
</script>

<template>
  <div class="pilot-stepper">
    <div class="pilot-stepper__head">
      <div class="pilot-stepper__head-left">
        <span class="pilot-stepper__icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
          </svg>
        </span>
        <span class="pilot-stepper__title">{{ card.title || t('pilot.stepper.title') }}</span>
      </div>
      <div v-if="card.description" class="pilot-stepper__desc">
        {{ card.description }}
      </div>
    </div>

    <div class="pilot-stepper__track">
      <div
        v-for="(s, idx) in steps"
        :key="s.id ?? idx"
        class="pilot-stepper__step"
        :class="`is-${s.status || 'wait'}`"
      >
        <div class="pilot-stepper__node">
          <div class="pilot-stepper__circle">
            <svg v-if="s.status === 'ok'" class="pilot-stepper__svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <polyline points="20 6 9 17 4 12" />
            </svg>
            <svg v-else-if="s.status === 'running'" class="pilot-stepper__svg is-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" aria-hidden="true">
              <path d="M21 12a9 9 0 1 1-6.219-8.56" />
            </svg>
            <svg v-else-if="s.status === 'warn'" class="pilot-stepper__svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" aria-hidden="true">
              <circle cx="12" cy="12" r="10" />
              <line x1="12" y1="8" x2="12" y2="12" />
              <line x1="12" y1="16" x2="12.01" y2="16" />
            </svg>
            <svg v-else-if="s.status === 'error'" class="pilot-stepper__svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" aria-hidden="true">
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
            <span v-else class="pilot-stepper__num">{{ idx + 1 }}</span>
          </div>
          <div v-if="idx < steps.length - 1" class="pilot-stepper__line" :class="{ 'is-active': s.status === 'ok' }"></div>
        </div>

        <div class="pilot-stepper__info">
          <div class="pilot-stepper__label-row">
            <span class="pilot-stepper__label">{{ s.title }}</span>
            <span v-if="s.badge" class="pilot-stepper__badge" :class="`is-${s.status || 'wait'}`">{{ s.badge }}</span>
          </div>
          <div v-if="s.detail" class="pilot-stepper__detail">{{ s.detail }}</div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.pilot-stepper {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 12px 14px;
  border-radius: var(--radius);
  background: var(--surface-panel);
  border: 1px solid var(--border-soft);
  margin-top: 4px;
  margin-bottom: 4px;
}

.pilot-stepper__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.pilot-stepper__head-left {
  display: inline-flex;
  align-items: center;
  gap: 7px;
}

.pilot-stepper__icon {
  width: 15px;
  height: 15px;
  color: var(--accent);
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.pilot-stepper__icon svg {
  width: 100%;
  height: 100%;
}

.pilot-stepper__title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  line-height: 1.2;
}

.pilot-stepper__desc {
  font-size: 11px;
  color: var(--text-muted);
}

.pilot-stepper__track {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  overflow-x: auto;
  scrollbar-width: none;
  padding-bottom: 2px;
}

.pilot-stepper__step {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-width: 110px;
  gap: 6px;
}

.pilot-stepper__node {
  display: flex;
  align-items: center;
  position: relative;
  width: 100%;
}

.pilot-stepper__circle {
  width: 22px;
  height: 22px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  background: var(--surface-chip);
  border: 1.5px solid var(--border-soft);
  color: var(--text-muted);
  font-size: 11px;
  font-weight: 600;
  z-index: 1;
  transition: all var(--transition);
}

.pilot-stepper__step.is-ok .pilot-stepper__circle {
  background: var(--success);
  border-color: var(--success);
  color: var(--ink-on-fill);
}

.pilot-stepper__step.is-running .pilot-stepper__circle {
  background: var(--accent-a15);
  border-color: var(--accent);
  color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-a20);
}

.pilot-stepper__step.is-warn .pilot-stepper__circle {
  background: var(--warning-a15);
  border-color: var(--warning);
  color: var(--warning);
}

.pilot-stepper__step.is-error .pilot-stepper__circle {
  background: var(--danger-a15);
  border-color: var(--danger);
  color: var(--danger);
}

.pilot-stepper__svg {
  width: 12px;
  height: 12px;
}

.pilot-stepper__svg.is-spin {
  animation: pstep-spin 1s linear infinite;
}

@keyframes pstep-spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

.pilot-stepper__num {
  font-size: 10px;
  line-height: 1;
}

.pilot-stepper__line {
  position: absolute;
  left: 22px;
  right: 0;
  top: 50%;
  height: 2px;
  background: var(--border-soft);
  transform: translateY(-50%);
  transition: background var(--transition);
}

.pilot-stepper__line.is-active {
  background: var(--success);
}

.pilot-stepper__info {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.pilot-stepper__label-row {
  display: flex;
  align-items: center;
  gap: 5px;
  flex-wrap: wrap;
}

.pilot-stepper__label {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
  line-height: 1.3;
}

.pilot-stepper__step.is-wait .pilot-stepper__label {
  color: var(--text-muted);
}

.pilot-stepper__badge {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 999px;
  background: var(--surface-chip);
  color: var(--text-secondary);
  border: 1px solid var(--border-soft);
  line-height: 1.2;
}

.pilot-stepper__badge.is-ok {
  background: var(--success-a15);
  color: var(--success);
  border-color: transparent;
}

.pilot-stepper__badge.is-running {
  background: var(--accent-a15);
  color: var(--accent);
  border-color: var(--accent);
}

.pilot-stepper__detail {
  font-size: 11px;
  color: var(--text-muted);
  line-height: 1.3;
}
</style>
