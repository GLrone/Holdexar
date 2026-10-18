<script setup lang="ts">
import { computed } from 'vue'
import type { PilotFamilyFacts } from '@/api/client'
import { useI18n } from '@/locales'
import HlImg from '@/components/ui/HlImg.vue'
import RegionFlag from '@/components/RegionFlag.vue'

const props = defineProps<{
  data: PilotFamilyFacts
}>()

const { t } = useI18n()
const c = computed(() => props.data)

function steamFriendCode(sid: string): string {
  try {
    const n = BigInt(sid)
    if (n > 76561197960265728n) {
      return String(n - 76561197960265728n)
    }
  } catch {
    // 格式异常兜底
  }
  return sid ? sid.slice(-6) : '—'
}

function familyMemberName(m: { steamid: string; name?: string | null }): string {
  return m.name || t('family.member.unnamed', { id: m.steamid.slice(-4) })
}
</script>

<template>
  <div class="hsui-family-card">
    <div class="pcard-header-bar">
      <div class="pcard-header-bar__title">
        <svg class="pcard-header-bar__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" />
          <circle cx="9" cy="7" r="4" />
          <path d="M23 21v-2a4 4 0 0 0-3-3.87" />
          <path d="M16 3.13a4 4 0 0 1 0 7.75" />
        </svg>
        <span>{{ t('pilot.rows.family') }}</span>
      </div>
    </div>

    <template v-if="c.bound">
      <div class="pcard-fam-list">
        <div
          v-for="m in c.members"
          :key="m.steamid"
          class="pcard-fam-item"
          :class="m.role === 'primary' ? 'pcard-fam-item--primary' : 'pcard-fam-item--member'"
        >
          <div class="pcard-fam-item__avatar">
            <HlImg :src="m.avatar || undefined" :alt="familyMemberName(m)" loading="lazy">
              <template #fallback>
                <span>{{ (familyMemberName(m) || '?').slice(0, 1) }}</span>
              </template>
            </HlImg>
          </div>

          <div class="pcard-fam-item__info">
            <div class="pcard-fam-item__top">
              <span class="pcard-fam-item__name" :title="familyMemberName(m)">{{ familyMemberName(m) }}</span>
              <span class="pcard-fam-item__role-badge" :class="m.role === 'primary' ? 'is-primary' : 'is-member'">
                {{ t(m.role === 'primary' ? 'family.role.primary' : 'family.role.family') }}
              </span>
            </div>
            <div class="pcard-fam-item__sub">
              <span>{{ t('family.member.friendCode', { code: steamFriendCode(m.steamid) }) }}</span>
            </div>
          </div>

          <div class="pcard-fam-item__region">
            <RegionFlag v-if="m.region" :code="m.region" compact />
            <span v-else class="pcard-fam-item__region-unset">
              {{ t('family.member.regionUnset') }}
            </span>
          </div>
        </div>
      </div>

      <div class="pcard-fam-footer">
        <span v-if="c.walletRegion" class="pcard-fam-footer__wallet">
          <RegionFlag :code="c.walletRegion" compact />
        </span>
        <span v-if="c.total != null && c.total > c.members.length" class="pcard-fam-footer__more">
          {{ t('pilot.facts.more', { n: c.total }) }}
        </span>
      </div>
    </template>
    <div v-else class="pcard-fam-unbound">{{ t('pilot.row.famUnbound') }}</div>
  </div>
</template>

<style scoped>
.hsui-family-card {
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

.pcard-fam-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.pcard-fam-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  background: var(--surface-inset, rgba(0, 0, 0, 0.2));
  border: 1px solid var(--border-soft, rgba(102, 192, 244, 0.08));
  border-radius: var(--radius-sm, 6px);
}

.pcard-fam-item__avatar {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  overflow: hidden;
  background: var(--surface-card-2, #2a475e);
  flex-shrink: 0;
}

.pcard-fam-item__info {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.pcard-fam-item__top {
  display: flex;
  align-items: center;
  gap: 6px;
}

.pcard-fam-item__name {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-primary, #d9e3ec);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.pcard-fam-item__role-badge {
  font-size: 10px;
  padding: 1px 4px;
  border-radius: 2px;
}

.pcard-fam-item__role-badge.is-primary {
  background: var(--accent-a20);
  color: var(--accent, #66c0f4);
}

.pcard-fam-item__role-badge.is-member {
  background: rgba(255, 255, 255, 0.08);
  color: var(--text-muted, #8f98a0);
}

.pcard-fam-item__sub {
  font-size: 10px;
  color: var(--text-dim, #5a7080);
}

.pcard-fam-item__region-unset {
  font-size: 10px;
  color: var(--text-dim, #5a7080);
}

.pcard-fam-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 11px;
}

.pcard-fam-unbound {
  font-size: 12px;
  color: var(--text-dim, #5a7080);
  text-align: center;
  padding: 12px 0;
}
</style>
