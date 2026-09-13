<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from '@/locales'

export interface HlStepperStep {
  title: string
  desc?: string
  /** 特性小标签 */
  feats?: string[]
}

const props = defineProps<{ steps: HlStepperStep[] }>()

const emit = defineEmits<{ done: []; 'update:step': [index: number] }>()

const current = ref(0)
const completed = ref(false)

const isLast = computed(() => current.value === props.steps.length - 1)

const { t } = useI18n()

/* 箭头与礼花留在组件侧（是视觉符号，不是文案）；只有词是词条。 */
const nextLabel = computed(() =>
  isLast.value ? `${t('common.finish')} 🎉` : `${t('common.next')} →`,
)

watch(current, (v) => emit('update:step', v))

function next() {
  if (isLast.value) completed.value = true
  else current.value += 1
}

function back() {
  if (current.value > 0) current.value -= 1
}

function jump(i: number) {
  if (i < current.value) current.value = i
}

function restart() {
  completed.value = false
  current.value = 0
}

const viewport = ref<HTMLElement | null>(null)
</script>

<template>
  <div class="hl-stepper" :class="{ 'is-completed': completed }">
    <div class="hl-stepper__panel">
      <div class="hl-stepper__indicators">
        <template v-for="(step, i) in steps" :key="i">
          <button
            type="button"
            class="hl-step-ind"
            :class="{ 'is-active': i === current && !completed, 'is-complete': i < current }"
            @click="jump(i)"
          >
            <span v-if="i < current" class="hl-step-check">✓</span>
            <span v-else-if="i === current" class="hl-step-dot" />
            <span v-else>{{ i + 1 }}</span>
          </button>
          <span
            v-if="i < steps.length - 1"
            class="hl-step-conn"
            :class="{ 'is-complete': i < current }"
          />
        </template>
      </div>

      <div ref="viewport" class="hl-stepper__viewport">
        <div class="hl-stepper__track" :style="{ transform: `translateX(-${current * 100}%)` }">
          <div v-for="(step, i) in steps" :key="i" class="hl-stepper__step">
            <h2>{{ step.title }}</h2>
            <p v-if="step.desc">{{ step.desc }}</p>
            <div v-if="step.feats?.length" class="hl-step-feats">
              <span v-for="f in step.feats" :key="f" class="hl-feat">{{ f }}</span>
            </div>
            <slot :step="step" :index="i" />
          </div>
        </div>
      </div>

      <div class="hl-stepper__footer">
        <div class="hl-stepper__nav" :class="current === 0 ? 'end' : 'spread'">
          <button v-if="current > 0" type="button" class="hl-step-back" @click="back">
            ← {{ t('common.prev') }}
          </button>
          <span v-if="current > 0" class="hl-step-prog">
            {{ current + 1 }} / {{ steps.length }}
          </span>
          <button type="button" class="hl-step-next" @click="next">
            {{ nextLabel }}
          </button>
        </div>
      </div>

      <div class="hl-stepper__done">
        <div class="hl-stepper__done-ic">✓</div>
        <h3>{{ t('stepper.doneTitle') }}</h3>
        <p>{{ t('stepper.doneHint') }}</p>
        <button type="button" class="hl-step-next" @click="restart">{{ t('common.restart') }}</button>
      </div>
    </div>
  </div>
</template>
