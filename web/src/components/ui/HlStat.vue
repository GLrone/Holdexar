<script setup lang="ts">
/**
 * KPI 统计卡。**项目里所有「一个大数字 + 一行说明」的卡片都走这个组件。**
 *
 * 取值档位：
 *   sm  数字 18px / padding 8×6  / radius-sm —— 6~7 列的密集行（家庭库、愿望单）
 *   md  数字 22px / padding 11×10/ radius    —— 4~6 列的常规行（family 各 tab、工具箱）
 *   lg  数字 26px / padding 18   / radius-lg —— 主视觉大数
 *
 * **默认居中**：KPI 卡是一排并列的数字，居中读数最快；左对齐仅用于卡内含长文本的
 * 场景（`align="left"`）。
 *
 * 数字一律 `font-variant-numeric: tabular-nums` + 等宽字体：一排卡里数字位数不同时，
 * 定宽数字才不会让每个卡的数字横向跳动。
 */
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    /** 说明文字（数字下方） */
    label: string
    /** 主数值。也可用默认插槽塞富内容（如「123 笔」带单位 span） */
    value?: string | number
    /** 第三行小字。需要富内容时用 #sub 插槽 */
    sub?: string
    /** 数值颜色。优先级高于 tone */
    color?: string
    /** 语义色档位。good/warn/bad 分别取 --success / --warning / --danger */
    tone?: 'neutral' | 'good' | 'warn' | 'bad'
    size?: 'sm' | 'md' | 'lg'
    /** 对齐。默认居中 */
    align?: 'center' | 'left'
    /** 0~100，底部细进度条；不传则不渲染 */
    progress?: number
  }>(),
  { value: '', sub: '', color: '', tone: 'neutral', size: 'md', align: 'center' },
)

/* 语义档位走通用令牌，不用 --rate-good ——那是**汇率模块**的「汇率好」，被工具箱的
   「退款合计」借去当绿色用，同一语义两个绿。此处统一到 --success。 */
const TONE: Record<string, string> = {
  neutral: 'var(--text-primary)',
  good: 'var(--success)',
  warn: 'var(--warning)',
  bad: 'var(--danger)',
}

const numColor = computed(() => props.color || TONE[props.tone] || TONE.neutral)
const barWidth = computed(() => `${Math.max(0, Math.min(100, props.progress ?? 0))}%`)
</script>

<template>
  <div class="hl-stat" :class="[`hl-stat--${size}`, align === 'left' ? 'is-left' : '']">
    <div class="hl-stat__num" :style="{ color: numColor }">
      <slot>{{ value }}</slot>
    </div>
    <div class="hl-stat__label">{{ label }}</div>
    <div v-if="sub || $slots.sub" class="hl-stat__sub">
      <slot name="sub">{{ sub }}</slot>
    </div>
    <div v-if="progress !== undefined" class="hl-stat__bar"><i :style="{ width: barWidth }" /></div>
  </div>
</template>

<style scoped>
.hl-stat {
  background: var(--surface-inset);
  border: 1px solid var(--line-1);
  text-align: center;
  position: relative;
  overflow: hidden;
  min-width: 0;
  transition: border-color var(--transition), transform var(--transition);
}
.hl-stat.is-left { text-align: left; }

.hl-stat__num {
  font-weight: 700;
  font-family: var(--font-mono);
  line-height: 1.15;
  letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.hl-stat__label { color: var(--text-muted); }
.hl-stat__sub { color: var(--text-dim); }
/* 数值被长文本顶掉时（如 22px 的成员名）允许缩一号，但绝不换行——
   一排卡里某张换行会把它自己撑高、整行跟着错位。 */
.hl-stat__label,
.hl-stat__sub {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ── 三档尺寸 ── */
.hl-stat--sm { padding: var(--space-3) var(--space-2); border-radius: var(--radius-sm); }
.hl-stat--sm .hl-stat__num { font-size: 18px; }
.hl-stat--sm .hl-stat__label { font-size: 10px; margin-top: 2px; }
.hl-stat--sm .hl-stat__sub { font-size: 9px; margin-top: 1px; }

.hl-stat--md { padding: 11px 10px; border-radius: var(--radius); }
.hl-stat--md .hl-stat__num { font-size: 22px; }
.hl-stat--md .hl-stat__label { font-size: 11px; margin-top: 4px; }
.hl-stat--md .hl-stat__sub { font-size: 9.5px; margin-top: 2px; }

.hl-stat--lg { padding: 18px; border-radius: var(--radius-lg); }
.hl-stat--lg .hl-stat__num { font-size: 26px; }
.hl-stat--lg .hl-stat__label { font-size: 11.5px; margin-top: 4px; }
.hl-stat--lg .hl-stat__sub { font-size: 11px; margin-top: 3px; }

.hl-stat__bar {
  height: 3px;
  border-radius: 2px;
  background: var(--surface-track);
  margin-top: var(--space-4);
  overflow: hidden;
}
.hl-stat__bar i {
  display: block;
  height: 100%;
  border-radius: 2px;
  background: var(--accent-fill);
  transition: width calc(var(--duration-5) * var(--motion-scale)) var(--ease-out);
}
</style>
