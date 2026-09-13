<script setup lang="ts">
/* 成员洞察（成员活跃度 + 家庭组健康分）：
   - 活跃分档 = 距该成员最后一次入库的天数（活跃<14 / 温热<60 / 冷淡<180 / 沉睡）；
   - 健康分 = 四档均值（100/75/50/25）能量环；
   - 月均入库 = 总入库 ÷ 首次入库以来的月数。 */
import { computed, onMounted } from 'vue'

import { useI18n, useLocaleFormat, type MessageKey } from '@/locales'
import { useFamilyStore } from '@/stores/familyLib'

const store = useFamilyStore()
const { t } = useI18n()
const fmt = useLocaleFormat()
onMounted(() => { if (!store.ready) void store.load() })

const activity = computed(() => store.memberActivity)

/** 活跃四档：标签 key + 语义色。
 *
 *  标签**只存 key**（`labelKey`），不存译文——模块级常量在加载时求值一次，
 *  存中文会把语言冻死（冻结陷阱）。渲染期 `t(s.labelKey)` 现取。
 *  注意与 store 的 `statusKey` 是两回事：那份是成员行右侧的短徽章（活跃），
 *  这份是分档统计格的带阈值标签（活跃<14天）。两者别混。
 *
 *  这里取**语义令牌**，不是 lib/familyColors.ts 的分类色板——两者用途不同：
 *  色板要的是「N 个成员一眼分开」，这四档要的是「好 → 差」这条语义梯度。
 *  原先 color 写死的是**深色主题**取值（#2ed573=深色 --success、#66c0f4=深色
 *  --accent、#f59e0b=深色 --warning），浅色主题下四档一直挂着深色的色。
 *  其中 cold 还写着 `var(--warn, #f59e0b)`——`--warn` 这个令牌**根本不存在**
 *  （真名 `--warning`），回退值永远生效，等于把 #f59e0b 焊死。
 *  同时删掉另一个从未被读过的 `ring` 字段：能量环走的是模板里的 ins-grad 渐变
 *  （原先它俩是同一语义两个定义，ring 那侧的令牌写法才是对的）。 */
const STATUS_META: Record<string, { labelKey: MessageKey; color: string }> = {
  active: { labelKey: 'famInsight.tier.active', color: 'var(--success)' },
  warm: { labelKey: 'famInsight.tier.warm', color: 'var(--accent)' },
  cold: { labelKey: 'famInsight.tier.cold', color: 'var(--warning)' },
  dormant: { labelKey: 'famInsight.tier.dormant', color: 'var(--text-dim)' },
}
const STATUS_KEYS = ['active', 'warm', 'cold', 'dormant'] as const

/** 分档计数格（标签与色同源于 STATUS_META，不再各写一份） */
const statusRows = computed(() =>
  STATUS_KEYS.map((k) => ({ n: activity.value.counts[k], ...STATUS_META[k]! })),
)

/** 状态徽章。底色用 color-mix 而不是在色值后拼 `'22'`：
 *  色值一旦从字面量换成 `var()`，`'var(--success)' + '22'` 就是一条废 CSS。 */
function statusStyle(status: string) {
  const c = STATUS_META[status]?.color ?? 'var(--text-dim)'
  return { background: `color-mix(in srgb, ${c} 13%, transparent)`, color: c }
}

/* 健康分能量环（SVG 环） */
const RING_R = 52
const RING_C = 2 * Math.PI * RING_R
const healthFrac = computed(() => activity.value.healthScore / 100)
</script>

<template>
  <div>
    <div v-if="store.error" class="lib-empty">{{ store.error }}</div>
    <div v-else-if="store.loading && !store.ready" class="lib-empty">{{ t('famInsight.empty.loading') }}</div>
    <div v-else-if="store.acquiredGames.length === 0" class="lib-empty">
      {{ t('famInsight.empty.noData') }}
    </div>

    <template v-else>
      <div class="ins-grid">
        <!-- 左：成员活跃度 -->
        <div class="ins-card ins-card--main">
          <div class="ins-card__title">{{ t('famInsight.activity.title') }}</div>
          <div class="ins-list">
            <div v-for="m in activity.members" :key="m.steamid" class="ins-row">
              <HlImg :src="m.avatarUrl" class="ins-row__ava" alt="" loading="lazy">
                <template #fallback>
                  <div class="ins-row__ava ins-row__ava--fallback">{{ (m.personaName || '?').slice(0, 1) }}</div>
                </template>
              </HlImg>
              <div class="ins-row__main">
                <div class="ins-row__name">{{ m.personaName || t('famInsight.memberFallback', { id: m.steamid.slice(-4) }) }}</div>
                <div class="ins-row__meta">
                  <!-- 行内 <b> 在词条值里、v-html 渲染（同 bills 的先例）；末段
                       「N 天前最后入库」/「从未入库」是另外两条整句，不是被切开的片段 -->
                  <span v-html="t('famInsight.member.meta', { total: m.total, avg: fmt.fixed(m.monthlyAvg, 1) })"></span> ·
                  {{ m.daysSinceLatest >= 0 ? t('famInsight.member.last', { n: m.daysSinceLatest }) : t('famInsight.member.never') }}
                </div>
              </div>
              <span class="ins-status" :style="statusStyle(m.status)">
                {{ t(m.statusKey) }}
              </span>
            </div>
          </div>
        </div>

        <!-- 右：健康分环 + 分档统计 -->
        <div class="ins-side">
          <div class="ins-card">
            <div class="ins-card__title">{{ t('famInsight.health.title') }}</div>
            <div class="ins-ring">
              <svg viewBox="0 0 130 130">
                <circle cx="65" cy="65" :r="RING_R" fill="none" stroke="var(--surface-chip-2)" stroke-width="12" />
                <circle
                  cx="65" cy="65" :r="RING_R" fill="none"
                  stroke="url(#ins-grad)" stroke-width="12" stroke-linecap="round"
                  :stroke-dasharray="`${healthFrac * RING_C} ${RING_C}`"
                  transform="rotate(-90 65 65)"
                />
                <defs>
                  <linearGradient id="ins-grad" x1="0" y1="0" x2="1" y2="1">
                    <!-- stop-color 走 style 而非 presentation 属性：SVG 属性不接受 var() -->
                    <stop offset="0%" style="stop-color: var(--success)" />
                    <stop offset="100%" style="stop-color: var(--accent)" />
                  </linearGradient>
                </defs>
                <text x="65" y="62" text-anchor="middle" class="ins-ring__num">{{ activity.healthScore }}</text>
                <text x="65" y="78" text-anchor="middle" class="ins-ring__lbl">{{ t('famInsight.health.ring') }}</text>
              </svg>
            </div>
          </div>

          <div class="ins-card">
            <div class="ins-card__title">{{ t('famInsight.tier.title') }}</div>
            <div class="ins-counts">
              <div v-for="s in statusRows" :key="s.labelKey" class="ins-count" :style="{ '--c': s.color }">
                <b>{{ s.n }}</b><span>{{ t(s.labelKey) }}</span>
              </div>
            </div>
            <div class="ins-note">
              {{ t('famInsight.note') }}
            </div>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.lib-empty { text-align: center; padding: 26px 16px; border: 1px dashed var(--border-soft); border-radius: var(--radius); background: var(--surface-inset); font-size: 12.5px; color: var(--text-secondary); }

.ins-grid { display: grid; grid-template-columns: minmax(0, 1.7fr) minmax(0, 1fr); gap: 10px; }
@media (max-width: 760px) { .ins-grid { grid-template-columns: 1fr; } }

.ins-card { background: var(--surface-inset); border: 1px solid var(--line-1); border-radius: var(--radius); padding: 10px 12px; }
.ins-card__title { font-size: 11.5px; font-weight: 600; color: var(--text-secondary); margin-bottom: 10px; }
.ins-side { display: flex; flex-direction: column; gap: 10px; }

.ins-row { display: flex; align-items: center; gap: 10px; padding: 7px 0; border-bottom: 1px solid var(--row-border); }
.ins-row:last-child { border-bottom: none; }
.ins-row__ava { width: 32px; height: 32px; border-radius: 50%; object-fit: cover; background: var(--surface-chip-2); flex-shrink: 0; }
.ins-row__ava--fallback { display: grid; place-items: center; font-size: 14px; color: var(--text-dim); }
.ins-row__main { flex: 1; min-width: 0; }
.ins-row__name { font-size: 12px; font-weight: 600; color: var(--text-primary); }
.ins-row__meta { font-size: 10.5px; color: var(--text-dim); margin-top: 2px; }
/* :deep() —— 这行的 <b> 由 v-html 注入，拿不到 scoped 属性（同 settings/Index.vue
   的第 2–4 步）。样式本身逐字未动。 */
.ins-row__meta :deep(b) { color: var(--text-secondary); font-family: var(--font-mono, monospace); }
.ins-status { font-size: 10px; font-weight: 700; padding: 2px 9px; border-radius: 999px; flex-shrink: 0; }

.ins-ring { display: grid; place-items: center; padding: 6px 0; }
.ins-ring svg { width: 118px; height: 118px; }
.ins-ring__num { font-size: 22px; font-weight: 700; fill: var(--text-primary); font-family: var(--font-mono, monospace); }
.ins-ring__lbl { font-size: 9px; fill: var(--text-dim); }

.ins-counts { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; }
.ins-count { border: 1px solid color-mix(in srgb, var(--c) 30%, transparent); background: color-mix(in srgb, var(--c) 8%, transparent); border-radius: 7px; padding: 8px; text-align: center; }
.ins-count b { font-size: 17px; color: var(--c); font-family: var(--font-mono, monospace); display: block; line-height: 1.2; }
.ins-count span { font-size: 9.5px; color: var(--text-dim); }

.ins-note { margin-top: 8px; font-size: 10px; color: var(--text-dim); line-height: 1.6; }
</style>
