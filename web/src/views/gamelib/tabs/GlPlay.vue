<script setup lang="ts">
/* 游玩动态（自 family 页迁入，框架 pa-* 标准迁移）：KPI + 成员折叠卡（近2周/总时长条 + 游戏格）。
   数据源：familyLib store（GetOwnedGames playtime_forever/2weeks 聚合）。 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { HlButton, HlIcon, HlStat } from '@/components/ui'
import { useI18n } from '@/locales'
import { memberColor, PALETTE } from '@/lib/familyColors'
import { useFamilyStore } from '@/stores/familyLib'

const router = useRouter()
const { t } = useI18n()
const store = useFamilyStore()
onMounted(() => { if (!store.ready) void store.load() })

const collapsed = ref<Set<string>>(new Set())
function toggle(sid: string) {
  const next = new Set(collapsed.value)
  if (next.has(sid)) next.delete(sid)
  else next.add(sid)
  collapsed.value = next
}

/** 成员游玩卡（真实数据）：近2周/总时长 + 近2周活跃游戏 top6 */
const playMembers = computed(() =>
  store.memberStats
    .map((m, i) => {
      const play = store.memberPlay[m.steamid] ?? []
      const recent = play.filter((p) => p.minutes2w > 0).slice(0, 6)
      const fallback = recent.length === 0 ? play.slice(0, 6) : recent
      return {
        steamid: m.steamid,
        name: m.personaName || t('famPlay.memberFallback', { id: m.steamid.slice(-4) }),
        avatarUrl: m.avatarUrl,
        color: memberColor(i),
        ownedCount: m.ownedCount,
        recentCount: play.filter((p) => p.minutes2w > 0).length,
        recentMin: m.playtime2wMin,
        totalMin: m.playtimeTotalMin,
        games: fallback.map((p) => {
          const g = store.games.find((x) => x.appid === p.appid)
          return {
            appid: p.appid,
            name: g?.name || `AppID ${p.appid}`,
            headerImage: g?.headerImage || null,
            recent: p.minutes2w,
            total: p.minutes,
          }
        }),
      }
    })
    .sort((a, b) => b.recentMin - a.recentMin),
)

const kpi = computed(() => {
  const totalMin = store.libraryKpi.totalMin
  const recent2w = store.memberStats.reduce((s, m) => s + m.playtime2wMin, 0)
  const memberCount = Math.max(store.members.length, 1)
  const top = playMembers.value[0]
  return {
    totalMin,
    recent2w,
    avgRecent: Math.round(recent2w / memberCount),
    topName: top?.name ?? '—',
    topRecentMin: top?.recentMin ?? 0,
    topPct: recent2w > 0 && top ? Math.round((top.recentMin / recent2w) * 1000) / 10 : 0,
  }
})

/* 单位 `h` / `kh` 不是语言中立的（中文 `128h`、英文 `128 hrs`），走共用词条。
   这里保留原有的分档判断——它决定「用哪一支」，是逻辑不是文案；只有返回值
   换成了词条。数字仍走 toFixed：`1.5h` 的位数是刻意压短的（KPI 卡要窄），
   不是本地化格式问题。 */
function fmtHours(minutes: number): string {
  if (!minutes) return t('common.hours', { h: 0 })
  const h = minutes / 60
  if (h < 10) return t('common.hours', { h: h.toFixed(1) })
  if (h < 1000) return t('common.hours', { h: h.toFixed(0) })
  return t('common.hoursK', { h: (h / 1000).toFixed(1) })
}
function maxTotal(): number {
  return Math.max(...playMembers.value.map((m) => m.totalMin), 1)
}
</script>

<template>
  <div data-section="gamelib.section.play">
    <div v-if="store.error" class="lib-empty">{{ store.error }}</div>
    <div v-else-if="store.loading && !store.ready" class="lib-empty">{{ t('famPlay.empty.loading') }}</div>
    <div v-else-if="playMembers.length === 0" class="lib-empty">{{ t('famPlay.empty.noPlay') }}</div>

    <template v-else>
      <div class="pa-kpi-row">
        <HlStat
          :color="PALETTE.teal"
          :style="{ borderColor: PALETTE.teal + '30' }"
          :label="t('famPlay.kpi.totalLabel')"
          :value="fmtHours(kpi.totalMin)"
          :sub="t('famPlay.kpi.totalSub')"
        />
        <HlStat
          :color="PALETTE.gold"
          :style="{ borderColor: PALETTE.gold + '30' }"
          :label="t('famPlay.kpi.recentLabel')"
          :value="fmtHours(kpi.recent2w)"
          :sub="t('famPlay.kpi.recentSub')"
        />
        <HlStat
          :color="PALETTE.blue"
          :style="{ borderColor: PALETTE.blue + '30' }"
          :label="t('famPlay.kpi.avgLabel')"
          :value="fmtHours(kpi.avgRecent)"
          :sub="t('famPlay.kpi.members', { n: store.members.length })"
        />
        <!-- 第四张的「数值」是**成员名**不是数字：原先为塞下长昵称写死 font-size:16px，
             与同排另外三张的 24px 不成排。改走 HlStat 的 md 档（22px），超出交给
             ellipsis，完整昵称放 title 悬停可见。 -->
        <HlStat
          :color="PALETTE.pink"
          :style="{ borderColor: PALETTE.pink + '30' }"
          :title="kpi.topName"
          :label="t('famPlay.kpi.topLabel')"
          :value="kpi.topName"
          :sub="t('famPlay.kpi.topShare', { h: fmtHours(kpi.topRecentMin), pct: kpi.topPct })"
        />
      </div>

      <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px">
        <span style="font-size: 11px; color: var(--text-dim)">{{ t('famPlay.source') }}</span>
        <HlButton art="outline" tone="blue" size="sm" :disabled="store.loading" :loading="store.loading" @click="store.load(true)">
          <HlIcon v-if="!store.loading" name="refresh" />
          {{ t('famPlay.action.refresh') }}
        </HlButton>
      </div>

      <div
        v-for="(m, i) in playMembers"
        :key="m.steamid"
        class="pa-member-card"
        :class="{ 'is-collapsed': collapsed.has(m.steamid) }"
        :style="{ borderLeftColor: m.color }"
      >
        <div class="pa-member-header" @click="toggle(m.steamid)">
          <HlImg :src="m.avatarUrl" class="pa-member-avatar-img" alt="" loading="lazy">
            <template #fallback>
              <div class="pa-member-ava" :style="{ background: `linear-gradient(135deg, ${m.color}, ${m.color}cc)` }">{{ m.name.slice(0, 1) }}</div>
            </template>
          </HlImg>
          <div class="pa-member-info">
            <div class="pa-member-name">
              {{ m.name }}
              <span v-if="i === 0 && m.recentMin > 0" class="tag tag--accent" style="font-size: 9px; margin-left: 4px">{{ t('famPlay.tag.mostActive') }}</span>
              <span v-else-if="m.recentMin === 0" class="tag tag--warning" style="font-size: 9px; margin-left: 4px">{{ t('famPlay.tag.noPlay2w') }}</span>
            </div>
            <!-- 整句一条词条、行内 <b> 写在值里，v-html 渲染：按标记边界切成
                 「前缀 / 数字 / 后缀」再拼，英文拼不出完整句子（同 bills 的先例）。 -->
            <div class="pa-member-sub" v-html="t('famPlay.member.sub', { owned: m.ownedCount, recent: m.recentCount })"></div>
          </div>
          <svg class="pa-chevron" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
        </div>
        <div v-show="!collapsed.has(m.steamid)" class="pa-bars-wrap">
          <div class="pa-bars">
            <div class="pa-bar-row"><span class="pa-bar-label">{{ t('famPlay.bar.recent') }}</span><div class="pa-bar-track"><div class="pa-bar-fill" :style="{ width: Math.min(100, (m.recentMin / Math.max(kpi.recent2w, 1)) * 100) + '%', background: `linear-gradient(90deg, ${m.color}, ${m.color}aa)` }"></div></div><span class="pa-bar-val" :style="{ color: m.color }">{{ fmtHours(m.recentMin) }}</span></div>
            <div class="pa-bar-row"><span class="pa-bar-label">{{ t('famPlay.bar.total') }}</span><div class="pa-bar-track"><div class="pa-bar-fill" :style="{ width: (m.totalMin / maxTotal()) * 100 + '%', background: `linear-gradient(90deg, ${PALETTE.gold}, ${PALETTE.amber})` }"></div></div><span class="pa-bar-val" :style="{ color: PALETTE.gold }">{{ fmtHours(m.totalMin) }}</span></div>
          </div>
          <div class="pa-game-grid">
            <div v-for="g in m.games" :key="g.appid" class="pa-game-item">
              <HlImg :src="g.headerImage" class="pa-game-img" loading="lazy" alt="">
                <template #fallback>
                  <div class="pa-game-icon">{{ g.name.slice(0, 1) }}</div>
                </template>
              </HlImg>
              <div class="pa-game-body">
                <router-link class="pa-game-name" :to="`/game/${g.appid}`">{{ g.name }}</router-link>
                <div class="pa-game-time"><span :style="{ color: m.color, fontWeight: 600 }">{{ fmtHours(g.recent) }}</span> / <span style="color: #fbbf24">{{ fmtHours(g.total) }}</span></div>
              </div>
            </div>
            <div v-if="m.games.length === 0" class="pa-game-item" style="grid-column: span 3"><div class="pa-empty">{{ t('famPlay.empty.noRecords') }}</div></div>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.lib-empty { text-align: center; padding: 26px 16px; border: 1px dashed var(--border-soft); border-radius: var(--radius); background: var(--surface-inset); font-size: 12.5px; color: var(--text-secondary); }
.pa-kpi-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-bottom: 10px; }
/* KPI 卡本体走 components/ui/HlStat.vue。原先四张卡的边框与数字色是**裸十六进制**
   （#06cfbe / #fbbf24 / #54a0ff / #ec4899），此处改为 familyColors 的具名色
   （PALETTE.teal / gold / blue / pink，取值逐字相同，不是改色）。 */
.pa-kpi-row :deep(.hl-stat) { border-radius: 10px; }
.pa-member-card { background: var(--surface-inset); border: 1px solid var(--line-1); border-left: 3px solid var(--accent); border-radius: 9px; padding: 10px 12px; margin-bottom: 8px; }
.pa-member-header { display: flex; align-items: center; gap: 8px; cursor: pointer; user-select: none; padding: 4px 6px; margin: -4px -6px; border-radius: 6px; transition: background 0.15s; }
.pa-member-header:hover { background: var(--hover-soft); }
.pa-member-ava { width: 30px; height: 30px; border-radius: 50%; display: grid; place-items: center; font-size: 13px; font-weight: 700; color: var(--text-on-fill); flex-shrink: 0; }
.pa-member-avatar-img { width: 30px; height: 30px; border-radius: 50%; object-fit: cover; flex-shrink: 0; }
.pa-member-info { flex: 1; min-width: 0; }
.pa-member-name { font-size: 13px; font-weight: 700; color: var(--text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pa-member-sub { font-size: 10px; color: var(--text-dim); margin-top: 1px; }
/* :deep() —— 这行的 <b> 由 v-html 注入，拿不到 scoped 属性，scoped 规则够不到
   （同 settings/Index.vue 的第 2–4 步）。样式本身逐字未动。 */
.pa-member-sub :deep(b) { color: var(--text-secondary); font-family: var(--font-mono, monospace); }
.pa-chevron { flex-shrink: 0; transition: transform 0.2s; color: var(--text-dim); }
.pa-member-card.is-collapsed .pa-chevron { transform: rotate(-90deg); }
.pa-bars { margin-top: 8px; display: flex; flex-direction: column; gap: 6px; }
.pa-bar-row { display: flex; align-items: center; gap: 8px; }
.pa-bar-label { width: 48px; font-size: 10px; color: var(--text-muted); flex-shrink: 0; }
.pa-bar-track { flex: 1; height: 7px; background: var(--surface-track); border-radius: 4px; overflow: hidden; }
.pa-bar-fill { height: 100%; border-radius: 4px; transition: width 0.6s var(--ease-out); }
.pa-bar-val { min-width: 52px; text-align: right; font-size: 10.5px; font-weight: 700; font-family: var(--font-mono, monospace); flex-shrink: 0; }
.pa-game-grid { margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--line-1); display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; }
.pa-game-item { display: flex; gap: 6px; padding: 5px 7px; background: var(--surface-chip-2); border: 1px solid var(--line-1); border-radius: 6px; min-width: 0; }
.pa-game-img { width: 28px; height: 28px; border-radius: 4px; object-fit: cover; background: var(--surface-inset); flex-shrink: 0; }
.pa-game-icon { width: 28px; height: 28px; border-radius: 4px; display: grid; place-items: center; font-size: 12px; background: var(--surface-inset); flex-shrink: 0; color: var(--text-dim); }
.pa-game-body { flex: 1; min-width: 0; }
.pa-game-name { font-size: 10px; font-weight: 600; color: var(--text-primary); text-decoration: none; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; display: block; }
.pa-game-name:hover { color: var(--accent); }
.pa-game-time { font-size: 9px; color: var(--text-dim); margin-top: 2px; font-family: var(--font-mono, monospace); white-space: nowrap; }
.pa-empty { text-align: center; font-size: 11px; color: var(--text-dim); padding: 12px; }
.tag--accent { background: var(--accent-a15); color: var(--accent); }
.tag--warning { background: color-mix(in srgb, var(--warning) 16%, transparent); color: var(--warning); }
</style>
