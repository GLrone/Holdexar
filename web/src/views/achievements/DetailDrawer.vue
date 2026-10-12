<script setup lang="ts">
/* 成就明细抽屉：单游戏奖杯清单（图标/名称/描述/稀有度/全服占比/解锁时刻）。
   已解锁在前（后端排序）；未解锁用灰图（无灰图资产时对彩图做灰度降级）；
   本作最稀有的已解锁成就挂角标。 */
import { computed, ref, watch } from 'vue'

import {
  HlButton,
  HlDrawer,
  HlEmpty,
  HlIcon,
  HlImg,
  HlPopconfirm,
  HlSegmented,
  HlSkeleton,
  HlTag,
  HlTextarea,
} from '@/components/ui'
import {
  achievementsApi,
  type AchievementDetailPayload,
  type AchievementItem,
  type AchievementNoteItem,
  type NoteMood,
  type RarityTier,
} from '@/api/client'
import type { MessageKey } from '@/locales'
import { useI18n } from '@/locales'
import TrophyMedal from '@/components/business/TrophyMedal.vue'
import { PALETTE } from '@/lib/familyColors'

const props = defineProps<{
  modelValue: boolean
  appid: number | null
  /** 目标账号（'' = 主账号）：明细同样按 steamid 隔离，切号时抽屉已收起 */
  account?: string
}>()
const emit = defineEmits<{ 'update:modelValue': [boolean] }>()

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const { t } = useI18n()
const detail = ref<AchievementDetailPayload | null>(null)
const loading = ref(false)
let seq = 0

async function fetchDetail(appid: number): Promise<void> {
  const my = ++seq
  loading.value = true
  detail.value = null
  try {
    const d = await achievementsApi.gameDetail(appid, props.account ?? '')
    if (my === seq) detail.value = d
  } catch {
    if (my === seq) detail.value = null
  } finally {
    if (my === seq) loading.value = false
  }
}

watch(
  () => [props.modelValue, props.appid, props.account] as const,
  ([v, appid]) => {
    if (v && appid) {
      noteOpenFor.value = ''
      void fetchDetail(appid)
      void fetchNotes()
    }
  },
)

/* 稀有度档位 → 奖杯立绘（传说→金杯 / 极稀有→银杯 / 稀有→铜杯；
   少见与常见不给杯——大众成就挂杯反而稀释稀有档的分量） */
const RARITY_TROPHY: Partial<Record<RarityTier, 'gold' | 'silver' | 'bronze'>> = {
  ultra: 'gold',
  very_rare: 'silver',
  rare: 'bronze',
}

/* 稀有度档位 → 颜色 + 词条 key（模块级只存 key，渲染期 t()） */
const RARITY_META: Record<RarityTier, { color: string; key: MessageKey }> = {
  ultra: { color: PALETTE.gold, key: 'achievements.rarity.ultra' },
  very_rare: { color: PALETTE.violet, key: 'achievements.rarity.very_rare' },
  rare: { color: PALETTE.blue, key: 'achievements.rarity.rare' },
  uncommon: { color: PALETTE.teal, key: 'achievements.rarity.uncommon' },
  common: { color: PALETTE.slate, key: 'achievements.rarity.common' },
  unknown: { color: PALETTE.slate, key: 'achievements.rarity.unknown' },
}

function fmtHours(minutes: number): string {
  if (!minutes) return t('common.hours', { h: 0 })
  const h = minutes / 60
  if (h < 10) return t('common.hours', { h: h.toFixed(1) })
  if (h < 1000) return t('common.hours', { h: h.toFixed(0) })
  return t('common.hoursK', { h: (h / 1000).toFixed(1) })
}

function fmtDate(ts: number): string {
  return new Date(ts * 1000).toLocaleDateString()
}

function globalPctTitle(a: AchievementItem): string {
  return a.globalPercent == null
    ? ''
    : t('achievements.drawer.globalPct', { pct: a.globalPercent.toFixed(1) })
}

/** 本作最稀有的已解锁成就（角标标记） */
const rarestHere = computed(() => {
  const earned = (detail.value?.achievements ?? []).filter(
    (a) => a.achieved && a.globalPercent != null,
  )
  if (earned.length < 2) return ''
  return earned.reduce((best, a) =>
    (a.globalPercent ?? 100) < (best.globalPercent ?? 100) ? a : best,
  ).imageName
})

const unlockedCount = computed(() => detail.value?.achievements.filter((a) => a.achieved).length ?? 0)
const lockedCount = computed(() => detail.value?.achievements.filter((a) => !a.achieved).length ?? 0)

/* ── 成就批注：锚定 (appid, imageName)，读写走 /achievements/notes；仅首评给经验 ── */
const NOTE_MAX = 140
const MOODS: NoteMood[] = ['love', 'wow', 'hard', 'fun']
const MOOD_KEY: Record<NoteMood, MessageKey> = {
  love: 'achievements.note.mood.love',
  wow: 'achievements.note.mood.wow',
  hard: 'achievements.note.mood.hard',
  fun: 'achievements.note.mood.fun',
}

const noteMap = ref(new Map<string, AchievementNoteItem>())
const noteOpenFor = ref('')
const draftContent = ref('')
const draftMood = ref<NoteMood | null>(null)
const noteSaving = ref(false)
const noteBonus = ref('')
const noteError = ref('')
let bonusTimer: ReturnType<typeof setTimeout> | undefined

const moodOptions = computed(() => MOODS.map((m) => ({ label: t(MOOD_KEY[m]), value: m })))

/* 点击已选中的心情即清空（心情是可选项） */
const moodModel = computed<string>({
  get: () => draftMood.value ?? '',
  set: (v) => {
    draftMood.value = v ? (v === draftMood.value ? null : (v as NoteMood)) : null
  },
})

function noteKey(imageName: string): string {
  return `${detail.value?.appid ?? props.appid ?? 0}:${imageName}`
}

const noteOf = (imageName: string): AchievementNoteItem | undefined =>
  noteMap.value.get(noteKey(imageName))

async function fetchNotes(): Promise<void> {
  try {
    const payload = await achievementsApi.notes(props.account ?? '')
    noteMap.value = new Map(payload.notes.map((n) => [`${n.appid}:${n.imageName}`, n]))
  } catch {
    noteMap.value = new Map()
  }
}

function toggleNote(imageName: string): void {
  if (noteOpenFor.value === imageName) {
    noteOpenFor.value = ''
    return
  }
  const existing = noteOf(imageName)
  draftContent.value = existing?.content ?? ''
  draftMood.value = existing?.mood ?? null
  noteBonus.value = ''
  noteError.value = ''
  noteOpenFor.value = imageName
}

function flashBonus(xp: number): void {
  window.clearTimeout(bonusTimer)
  noteBonus.value = t('achievements.note.firstBonus', { xp })
  bonusTimer = window.setTimeout(() => {
    noteBonus.value = ''
  }, 4000)
}

async function saveNote(imageName: string): Promise<void> {
  const appid = detail.value?.appid
  const content = draftContent.value.trim()
  if (!appid || !content || noteSaving.value) return
  noteSaving.value = true
  noteError.value = ''
  try {
    const r = await achievementsApi.addNote({
      steamid: props.account ?? '',
      appid,
      imageName,
      content,
      mood: draftMood.value,
    })
    noteMap.value.set(`${r.note.appid}:${r.note.imageName}`, r.note)
    if (r.xpGranted > 0) flashBonus(r.xpGranted)
  } catch {
    noteError.value = t('achievements.note.saveFailed')
  } finally {
    noteSaving.value = false
  }
}

async function removeNote(imageName: string): Promise<void> {
  const appid = detail.value?.appid
  if (!appid) return
  try {
    await achievementsApi.deleteNote(appid, imageName, props.account ?? '')
    noteMap.value.delete(noteKey(imageName))
    draftContent.value = ''
    draftMood.value = null
    noteBonus.value = ''
  } catch {
    noteError.value = t('achievements.note.saveFailed')
  }
}
</script>

<template>
  <HlDrawer v-model="visible" width="580px" :with-header="false">
    <div class="adw-body">
      <button class="adw-close" :title="t('common.close')" @click="visible = false">✕</button>

      <template v-if="loading">
        <HlSkeleton variant="text" :count="1" :rows="3" />
        <HlSkeleton variant="text" :count="5" :rows="2" />
      </template>

      <template v-else-if="detail">
        <div class="adw-head">
          <div class="adw-banner-box">
            <HlImg :src="detail.headerImage" class="adw-banner" alt="">
              <template #fallback>
                <div class="adw-banner-fb">{{ detail.name.slice(0, 1) }}</div>
              </template>
            </HlImg>
          </div>
          <div class="adw-head-info">
            <div class="adw-title">
              <span class="adw-title-text">{{ detail.name }}</span>
              <HlTag v-if="!detail.owned" type="info" class="adw-ext-tag">
                {{ t('achievements.list.externalTag') }}
              </HlTag>
              <HlTag v-if="detail.platinum" type="warning" class="adw-plat-tag">
                <TrophyMedal tier="platinum" :size="18" />
                {{ t('achievements.list.platinumTag') }}
              </HlTag>
            </div>
            <div class="adw-meta">
              <span>
                {{
                  detail.owned
                    ? t('achievements.drawer.playtime', { h: fmtHours(detail.playtimeMin) })
                    : t('achievements.list.externalHours')
                }}
              </span>
              <template v-if="detail.perfectDate > 0">
                <span class="adw-meta-dot">·</span>
                <span>{{ t('achievements.drawer.perfectDate', { date: fmtDate(detail.perfectDate) }) }}</span>
              </template>
            </div>
            <div class="adw-bar">
              <i :style="{ width: detail.percent + '%' }" />
            </div>
            <div class="adw-count hl-num">
              {{ t('achievements.drawer.unlockedOf', { unlocked: detail.unlocked, total: detail.total }) }}
              · {{ detail.percent.toFixed(1) }}%
            </div>
          </div>
        </div>

        <HlEmpty
          v-if="!detail.achievements.length"
          size="sm"
          icon=""
          :text="t('achievements.drawer.empty')"
        />
        <template v-else>
          <div class="adw-groups">
            <span class="adw-group is-on">{{ t('achievements.drawer.groupUnlocked', { n: unlockedCount }) }}</span>
            <span class="adw-group">{{ t('achievements.drawer.groupLocked', { n: lockedCount }) }}</span>
          </div>
          <div class="adw-list">
            <div v-for="a in detail.achievements" :key="a.imageName" class="adw-item-wrap">
              <div class="adw-item" :class="{ 'is-locked': !a.achieved }">
                <div class="adw-icon-box">
                  <HlImg
                    :src="a.achieved ? a.icon : a.iconGray || a.icon"
                    class="adw-icon"
                    :class="{ 'is-gray': !a.achieved && !a.iconGray }"
                    alt=""
                    loading="lazy"
                  >
                    <template #fallback>
                      <div class="adw-icon-fb"><TrophyMedal tier="platinum" :size="30" /></div>
                    </template>
                  </HlImg>
                </div>
                <div class="adw-item-main">
                  <div class="adw-item-name" :title="a.name">
                    <span class="adw-item-name-text">{{ a.name }}</span>
                    <span
                      v-if="a.imageName === rarestHere"
                      class="adw-item-rarest"
                      :title="globalPctTitle(a)"
                    >
                      {{ t('achievements.highlight.rarestBadge') }}
                    </span>
                  </div>
                  <div class="adw-item-desc" :title="a.description">{{ a.description }}</div>
                </div>
                <div class="adw-item-side">
                  <span
                    class="adw-rarity"
                    :style="{ color: RARITY_META[a.rarity].color, borderColor: RARITY_META[a.rarity].color + '55' }"
                    :title="globalPctTitle(a)"
                  >
                  <TrophyMedal
                    v-if="RARITY_TROPHY[a.rarity]"
                    :tier="RARITY_TROPHY[a.rarity]"
                    :size="14"
                  />
                    {{ t(RARITY_META[a.rarity].key) }}
                  </span>
                  <span class="adw-state" :class="{ 'is-on': a.achieved }">
                    {{
                      a.achieved
                        ? t('achievements.drawer.unlockAt', { date: fmtDate(a.unlockTime) })
                        : t('achievements.drawer.locked')
                    }}
                  </span>
                  <button
                    type="button"
                    class="adw-note-btn"
                    :class="{ 'is-on': !!noteOf(a.imageName) }"
                    :title="t('achievements.social.tab.notes')"
                    @click.stop="toggleNote(a.imageName)"
                  >
                    <HlIcon name="edit" :size="12" />
                  </button>
                </div>
              </div>

              <div v-if="noteOpenFor === a.imageName" class="adw-note">
                <HlTextarea
                  v-model="draftContent"
                  :rows="2"
                  :maxlength="NOTE_MAX"
                  :placeholder="t('achievements.note.placeholder')"
                />
                <div class="adw-note-tools">
                  <HlSegmented v-model="moodModel" :options="moodOptions" />
                  <span class="adw-note-count hl-num">
                    {{ t('achievements.note.maxLen', { used: draftContent.length, max: NOTE_MAX }) }}
                  </span>
                  <span v-if="noteBonus" class="adw-note-bonus">{{ noteBonus }}</span>
                </div>
                <div class="adw-note-actions">
                  <span class="adw-note-hint">{{ t('achievements.note.firstBonusHint') }}</span>
                  <span class="adw-note-btns">
                    <HlPopconfirm
                      v-if="noteOf(a.imageName)"
                      :text="t('achievements.note.confirmDelete')"
                      @confirm="removeNote(a.imageName)"
                    >
                      <HlButton variant="ghost" size="sm">
                        {{ t('achievements.note.delete') }}
                      </HlButton>
                    </HlPopconfirm>
                    <HlButton
                      variant="primary"
                      size="sm"
                      :disabled="!draftContent.trim()"
                      :loading="noteSaving"
                      @click="saveNote(a.imageName)"
                    >
                      {{ t('achievements.note.save') }}
                    </HlButton>
                  </span>
                </div>
                <p v-if="noteError" class="adw-note-err">{{ noteError }}</p>
              </div>
            </div>
          </div>
        </template>
      </template>

      <HlEmpty v-else size="sm" icon="" :text="t('achievements.drawer.empty')" />
    </div>
  </HlDrawer>
</template>

<style scoped>
.adw-body { padding: 14px 16px 18px; position: relative; }
.adw-close {
  position: absolute; top: 10px; right: 12px; z-index: 2;
  width: 26px; height: 26px; border-radius: 6px;
  border: 1px solid var(--line-1); background: var(--surface-inset);
  color: var(--text-secondary); font-size: 12px; cursor: pointer;
  transition: background calc(var(--duration-1) * var(--motion-scale)) var(--ease-out);
}
.adw-close:hover { background: var(--hover-soft); }

.adw-head { display: flex; gap: 12px; margin-bottom: 10px; }
.adw-banner-box { width: 184px; height: 86px; border-radius: 6px; overflow: hidden; flex-shrink: 0; background: var(--surface-track); }
.adw-banner { width: 100%; height: 100%; object-fit: cover; display: block; }
.adw-banner-fb { width: 100%; height: 100%; display: grid; place-items: center; font-size: 26px; font-weight: 700; color: var(--text-dim); }
.adw-head-info { flex: 1; min-width: 0; display: flex; flex-direction: column; justify-content: center; gap: 4px; }
.adw-title { display: flex; align-items: center; gap: 6px; min-width: 0; }
.adw-plat-tag { display: inline-flex; align-items: center; gap: 4px; }
.adw-title-text { font-size: 15px; font-weight: 700; color: var(--text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.adw-meta { font-size: 10.5px; color: var(--text-dim); display: flex; gap: 5px; flex-wrap: wrap; }
.adw-meta-dot { color: var(--text-dim); }
.adw-bar { height: 6px; border-radius: 3px; background: var(--surface-track); overflow: hidden; }
.adw-bar i { display: block; height: 100%; border-radius: 3px; background: var(--accent-fill); }
.adw-count { font-size: 11px; color: var(--text-secondary); }

.adw-groups { display: flex; gap: 8px; margin-bottom: 7px; }
.adw-group { font-size: 10px; color: var(--text-dim); border: 1px solid var(--line-1); border-radius: 4px; padding: 1px 7px; }
.adw-group.is-on { color: var(--success); border-color: color-mix(in srgb, var(--success) 35%, transparent); }

.adw-list { display: flex; flex-direction: column; gap: 5px; max-height: calc(100vh - 250px); overflow-y: auto; }
.adw-item {
  display: flex; align-items: center; gap: 10px;
  background: var(--surface-inset); border: 1px solid var(--line-1);
  border-radius: var(--radius-sm); padding: 7px 10px; min-width: 0;
}
.adw-item.is-locked { opacity: 0.62; }
.adw-icon-box { width: 40px; height: 40px; border-radius: 5px; overflow: hidden; flex-shrink: 0; background: var(--surface-track); }
.adw-icon { width: 100%; height: 100%; object-fit: cover; display: block; }
/* 无灰图资产时对彩图做灰度降级（等价 Steam 的 _BW 资产） */
.adw-icon.is-gray { filter: grayscale(1) brightness(0.72); }
.adw-icon-fb { width: 100%; height: 100%; display: grid; place-items: center; font-size: 16px; }
.adw-item-main { flex: 1; min-width: 0; }
.adw-item-name { display: flex; align-items: center; gap: 5px; min-width: 0; }
.adw-item-name-text { font-size: 12px; font-weight: 600; color: var(--text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.adw-item-rarest { flex-shrink: 0; font-size: 8.5px; font-weight: 700; color: var(--warning); background: color-mix(in srgb, var(--warning) 14%, transparent); border-radius: 3px; padding: 0 4px; line-height: 14px; }
.adw-item-desc { font-size: 10.5px; color: var(--text-dim); margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.adw-item-side { flex-shrink: 0; display: flex; flex-direction: column; align-items: flex-end; gap: 4px; }
.adw-rarity {
  display: inline-flex; align-items: center; gap: 3px;
  font-size: 9.5px; font-weight: 700; line-height: 1;
  border: 1px solid transparent; border-radius: 4px; padding: 2px 6px;
}
.adw-state { font-size: 9.5px; color: var(--text-dim); max-width: 130px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.adw-state.is-on { color: var(--success); }

/* 成就批注：行侧入口 + 行下展开的读写面板 */
.adw-item-wrap { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.adw-note-btn {
  display: inline-flex; align-items: center; justify-content: center;
  width: 24px; height: 18px; border-radius: 4px;
  border: 1px solid var(--line-1); background: var(--surface-inset);
  color: var(--text-dim); cursor: pointer;
  transition: color calc(var(--duration-1) * var(--motion-scale)) var(--ease-out),
    border-color calc(var(--duration-1) * var(--motion-scale)) var(--ease-out),
    background calc(var(--duration-1) * var(--motion-scale)) var(--ease-out);
}
.adw-note-btn:hover { color: var(--accent); border-color: var(--accent-a45); }
.adw-note-btn.is-on { color: var(--accent); border-color: var(--accent-a45); background: color-mix(in srgb, var(--accent) 12%, transparent); }
.adw-note {
  display: flex; flex-direction: column; gap: 6px;
  margin-left: 50px; padding: 8px 10px;
  border: 1px dashed var(--line-1); border-radius: var(--radius-sm);
  background: var(--surface-inset-sm);
}
.adw-note-tools { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.adw-note-count { font-size: 10px; color: var(--text-dim); }
.adw-note-bonus { font-size: 10px; font-weight: 700; color: var(--success); }
.adw-note-actions { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.adw-note-hint { font-size: 10px; color: var(--text-dim); }
.adw-note-btns { display: inline-flex; align-items: center; gap: 6px; }
.adw-note-err { margin: 0; font-size: 10.5px; color: var(--danger); }
</style>