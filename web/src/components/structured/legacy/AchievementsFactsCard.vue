<script setup lang="ts">
import { computed } from 'vue'
import type { PilotAchievementFacts } from '@/api/client'
import { useI18n } from '@/locales'
import { pilotCoverUrl } from '@/lib/pilotView'
import HlImg from '@/components/ui/HlImg.vue'
import { dateOf } from '../helpers'

const props = defineProps<{
  data: PilotAchievementFacts
}>()

const { t } = useI18n()
const c = computed(() => props.data)
</script>

<template>
  <div class="hsui-achievements-card">
    <div class="pcard-header-bar">
      <div class="pcard-header-bar__title">
        <svg class="pcard-header-bar__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <circle cx="12" cy="8" r="7" />
          <polyline points="8.21 13.89 7 23 12 20 17 23 15.79 13.88" />
        </svg>
        <span>{{ t('pilot.rows.achievements') }}</span>
      </div>
    </div>

    <template v-if="c.hasCredential">
      <div class="pcard-ach-kpi">
        <div class="pcard-ach-kpi__left">
          <span class="pcard-ach-kpi__plat">{{ t('pilot.card.achPlatinum', { n: c.platinum }) }}</span>
        </div>
        <div class="pcard-ach-kpi__right">
          <span class="pcard-ach-kpi__pct">{{ t('pilot.card.achRate', { rate: c.completionRate ?? 0 }) }}</span>
        </div>
      </div>

      <div class="pcard-ach-progress">
        <i class="pcard-ach-progress__fill" :style="{ width: `${Math.max(0, Math.min(100, c.completionRate ?? 0))}%` }"></i>
      </div>

      <div class="pcard-ach-stats-text">
        {{ t('pilot.row.achLine', { platinum: c.platinum, unlocked: c.unlocked, total: c.total }) }}
      </div>

      <div v-if="c.platinums?.length" class="pcard-ach-shelf">
        <router-link
          v-for="p in c.platinums"
          :key="p.appid"
          class="pcard-ach-game-slot"
          :to="`/game/${p.appid}`"
          :title="p.name || `AppID ${p.appid}`"
        >
          <HlImg :src="pilotCoverUrl(p.appid)" :alt="p.name || `AppID ${p.appid}`" loading="lazy">
            <template #fallback>
              <span>{{ (p.name || `AppID ${p.appid}`).slice(0, 2) }}</span>
            </template>
          </HlImg>
        </router-link>
      </div>

      <div v-if="c.recent?.length" class="pcard-ach-recent">
        <div class="pcard-ach-recent__title">{{ t('pilot.card.recentUnlocks') }}</div>
        <div v-for="a in c.recent" :key="`${a.appid}-${a.name}`" class="pcard-ach-recent__item">
          <router-link
            v-if="a.appid"
            :to="`/game/${a.appid}`"
            class="pcard-ach-recent__cover-link"
            :title="a.gameName || `AppID ${a.appid}`"
          >
            <HlImg
              :src="pilotCoverUrl(a.appid)"
              :alt="a.gameName || `AppID ${a.appid}`"
              loading="lazy"
              class="pcard-ach-recent__cover"
            >
              <template #fallback>
                <span class="pcard-ach-recent__cover-fallback">{{ (a.gameName || `AppID ${a.appid}`).slice(0, 1) }}</span>
              </template>
            </HlImg>
          </router-link>
          <div class="pcard-ach-recent__main">
            <span class="pcard-ach-recent__name">{{ a.name }}</span>
            <span class="pcard-ach-recent__game" :title="a.gameName || `AppID ${a.appid}`">{{ a.gameName || `AppID ${a.appid}` }}</span>
          </div>
          <span v-if="a.at" class="pcard-ach-recent__date">{{ dateOf(new Date((a.at as number) * 1000).toISOString()) }}</span>
        </div>
      </div>
    </template>
    <div v-else class="pcard-fam-unbound">{{ t('pilot.card.achNoCredential') }}</div>
  </div>
</template>

<style scoped>
.hsui-achievements-card {
  display: flex;
  flex-direction: column;
  gap: 10px;
  width: 100%;
}

.pcard-header-bar {
  display: flex;
  align-items: center;
  gap: 8px;
}

.pcard-header-bar__title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary, #d9e3ec);
}

.pcard-header-bar__icon {
  width: 15px;
  height: 15px;
  color: var(--accent, #66c0f4);
}

.pcard-ach-kpi {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
}

.pcard-ach-kpi__plat {
  font-size: 16px;
  font-weight: 700;
  color: var(--accent, #66c0f4);
}

.pcard-ach-kpi__pct {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-secondary, #c7d5e0);
}

.pcard-ach-progress {
  height: 4px;
  background: var(--surface-track, rgba(255, 255, 255, 0.12));
  border-radius: 2px;
  overflow: hidden;
}

.pcard-ach-progress__fill {
  display: block;
  height: 100%;
  background: linear-gradient(90deg, var(--accent, #66c0f4), var(--success, #a4d007));
}

.pcard-ach-stats-text {
  font-size: 11px;
  color: var(--text-muted, #8f98a0);
}

.pcard-ach-shelf {
  display: flex;
  align-items: center;
  gap: 6px;
  overflow-x: auto;
  padding: 4px 0;
}

.pcard-ach-game-slot {
  width: 52px;
  height: 28px;
  border-radius: 4px;
  overflow: hidden;
  background: var(--surface-card-2, #2a475e);
  flex-shrink: 0;
}

.pcard-ach-game-slot img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.pcard-ach-recent {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 4px;
}

.pcard-ach-recent__title {
  font-size: 11px;
  color: var(--text-dim, #5a7080);
}

.pcard-ach-recent__item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 6px;
  background: var(--surface-inset, rgba(0, 0, 0, 0.2));
  border-radius: var(--radius-sm, 6px);
}

.pcard-ach-recent__cover {
  width: 32px;
  height: 18px;
  border-radius: 2px;
  object-fit: cover;
}

.pcard-ach-recent__main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.pcard-ach-recent__name {
  font-size: 11px;
  font-weight: 500;
  color: var(--text-primary, #d9e3ec);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.pcard-ach-recent__game {
  font-size: 10px;
  color: var(--text-dim, #5a7080);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.pcard-ach-recent__date {
  font-size: 10px;
  color: var(--text-dim, #5a7080);
}

.pcard-fam-unbound {
  font-size: 12px;
  color: var(--text-dim, #5a7080);
  text-align: center;
  padding: 12px 0;
}
</style>
