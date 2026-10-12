<script setup lang="ts">
/* 猎手批注墙：锚定到具体成就的短评卡片（数据出自 achievements_notes 账本，
   与生涯便利贴墙视觉同族、账本独立）。mood 四态筛选；点击卡片抛 open-game
   给宿主开对应游戏的成就抽屉。 */
import { computed, onMounted, ref } from 'vue'

import type { NoteMood } from '@/api/client'
import { HlEmpty, HlIcon, HlImg, HlSegmented, type HlSelectOption } from '@/components/ui'
import type { MessageKey } from '@/locales'
import { useI18n } from '@/locales'
import { PALETTE } from '@/lib/familyColors'
import { vStagger } from '@/lib/stagger'
import TrophyMedal from '@/components/business/TrophyMedal.vue'
import { useTrophyHallStore } from '@/stores/trophyHall'

const emit = defineEmits<{ 'open-game': [appid: number] }>()

const { t } = useI18n()
const hall = useTrophyHallStore()

onMounted(() => {
  void hall.load()
})

const MOODS: NoteMood[] = ['love', 'wow', 'hard', 'fun']

const MOOD_KEY: Record<NoteMood, MessageKey> = {
  love: 'achievements.note.mood.love',
  wow: 'achievements.note.mood.wow',
  hard: 'achievements.note.mood.hard',
  fun: 'achievements.note.mood.fun',
}

const MOOD_COLOR: Record<NoteMood, string> = {
  love: PALETTE.salmon,
  wow: PALETTE.gold,
  hard: PALETTE.violet,
  fun: PALETTE.teal,
}

const MOOD_ICON: Record<NoteMood, string> = {
  love: 'heart',
  wow: 'star',
  hard: 'target',
  fun: 'gamepad',
}

const mood = ref<string>('all')

const moodOptions = computed<HlSelectOption[]>(() => [
  { label: t('achievements.filter.all'), value: 'all' },
  ...MOODS.map((m) => ({ label: t(MOOD_KEY[m]), value: m })),
])

const notes = computed(() => hall.notes)

const shown = computed(() =>
  mood.value === 'all' ? notes.value : notes.value.filter((n) => n.mood === mood.value),
)

const moodTone = (mood: NoteMood | null): string => (mood ? MOOD_COLOR[mood] : '')
</script>

<template>
  <div class="cr-card so-notes">
    <div class="cr-head">
      <span class="cr-head-title">
        <HlIcon name="edit" />
        {{ t('achievements.note.title') }}
      </span>
      <span class="cr-head-hint hl-num">
        {{ t('achievements.note.count', { n: notes.length }) }}
      </span>
    </div>

    <HlSegmented v-model="mood" :options="moodOptions" class="so-notes-filter" />

    <HlSkeleton v-if="hall.loading && !notes.length" variant="text" :count="4" :rows="1" />
    <HlEmpty v-else-if="!notes.length" size="sm" icon="" :text="t('achievements.note.empty')" />
    <HlEmpty v-else-if="!shown.length" size="sm" icon="" :text="t('achievements.note.noneMood')" />
    <div v-else class="so-notes-grid hl-stagger" v-stagger>
      <button
        v-for="n in shown"
        :key="`${n.appid}-${n.imageName}`"
        type="button"
        class="so-note"
        :style="{ '--so-mood': moodTone(n.mood) || 'var(--line-1)' }"
        @click="emit('open-game', n.appid)"
      >
        <div class="so-note-icon">
          <HlImg :src="n.achievementIcon" class="so-note-img" alt="" loading="lazy">
            <template #fallback>
              <div class="so-note-fb"><TrophyMedal tier="platinum" :size="26" /></div>
            </template>
          </HlImg>
        </div>
        <div class="so-note-body">
          <p class="so-note-text" :title="n.content">{{ n.content }}</p>
          <div class="so-note-meta">
            <span
              v-if="n.mood"
              class="so-note-mood"
              :style="{ color: MOOD_COLOR[n.mood], background: `color-mix(in srgb, ${MOOD_COLOR[n.mood]} 12%, transparent)` }"
            >
              <HlIcon :name="MOOD_ICON[n.mood]" :size="11" />
              {{ t(MOOD_KEY[n.mood]) }}
            </span>
            <span class="so-note-ach" :title="n.achievementName">{{ n.achievementName }}</span>
          </div>
          <span class="so-note-game" :title="n.gameName">{{ n.gameName }}</span>
        </div>
      </button>
    </div>
  </div>
</template>

<style src="../career/career-shared.css"></style>

<style scoped>
.so-notes-filter {
  margin-bottom: 10px;
}

.so-notes-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(228px, 1fr));
  gap: 8px;
}

/* 卡片骨架与生涯箴言墙同族：左缘 mood 色条 + 图标锚点 + 短文 */
.so-note {
  display: flex;
  gap: 8px;
  padding: 9px 10px;
  border: 1px solid var(--line-1);
  border-left: 3px solid var(--so-mood);
  border-radius: var(--radius-sm);
  background: var(--surface-inset-sm);
  cursor: pointer;
  min-width: 0;
  text-align: left;
  transition: transform calc(var(--duration-2) * var(--motion-scale)) var(--ease-out),
    border-color calc(var(--duration-2) * var(--motion-scale)) var(--ease-out);
}

.so-note:hover {
  transform: translateY(-2px);
  border-color: var(--so-mood);
}

.so-note-icon {
  flex: 0 0 auto;
  width: 34px;
  height: 34px;
  border-radius: var(--radius-sm);
  overflow: hidden;
  background: var(--surface-track);
}

.so-note-img {
  width: 34px;
  height: 34px;
  object-fit: cover;
  display: block;
}

.so-note-fb {
  width: 34px;
  height: 34px;
  display: grid;
  place-items: center;
}

.so-note-body {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}

.so-note-text {
  margin: 0;
  font-size: 11.5px;
  line-height: 1.45;
  color: var(--text-primary);
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.so-note-meta {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 9.5px;
  color: var(--text-dim);
  min-width: 0;
}

.so-note-mood {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  gap: 3px;
  border-radius: 4px;
  padding: 1px 5px;
  font-weight: 700;
  line-height: 1.2;
}

.so-note-ach {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.so-note-game {
  font-size: 9.5px;
  color: var(--text-dim);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
