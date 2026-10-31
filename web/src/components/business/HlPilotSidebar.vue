<template>
  <Teleport defer to="#pilot-side-target">
    <aside class="pilot-side">
      <div class="pilot-side__tools">
        <button
          type="button"
          class="pilot-side__new"
          :class="{ 'is-current': freshChat }"
          :disabled="streaming"
          @click="emit('new')"
        >
          <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <path d="M12 5v14M5 12h14" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" />
          </svg>
          {{ t('pilot.newChat') }}
        </button>
        <HlPilotRuns variant="side" />
        <HlPilotOutline
          side="right"
          :turns="turns"
          :count="outlineCount"
          :active-index="outlineActive"
          @jump="(i: number) => emit('jump', i)"
          @toggle="emit('outline-toggle')"
        />
      </div>
      <div class="pilot-side__search">
        <HlInput
          v-model="query"
          class="pilot-side__search-box"
          :placeholder="t('pilot.session.search')"
          :aria-label="t('pilot.session.search')"
          @input="onQueryInput"
        />
      </div>
      <div class="pilot-side__scroll">
        <div v-if="sessions == null" class="pilot-side__empty">{{ t('pilot.history.empty') }}</div>
        <div v-else-if="!sessions.length" class="pilot-side__empty">
          {{ searching ? t('pilot.session.noHit') : t('pilot.history.empty') }}
        </div>
        <template v-else>
          <template v-for="row in rows" :key="row.head ? '__arch' : row.item?.sid">
            <!-- 归档折叠头：点开才列归档项（检索态不出现，命中结果平铺） -->
            <button
              v-if="row.head"
              type="button"
              class="pilot-side__arch"
              @click="archivedOpen = !archivedOpen"
            >
              <svg class="pilot-side__arch-ico" :class="{ 'is-open': archivedOpen }" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <path d="M9 6l6 6-6 6" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
              <span>{{ t('pilot.session.archived', { n: row.count ?? 0 }) }}</span>
            </button>
            <div
              v-else
              class="pilot-side__row"
              :class="{ 'is-current': row.item?.sid === sessionId, 'is-arch': row.item?.archived }"
              :title="`${labelOf(row.item ?? {})} · ${t('pilot.history.turns', { n: row.item?.turn_total ?? 0 })}${row.item?.running ? ' · ' + t('pilot.session.running') : ''}`"
              @click="row.item && emit('open', row.item.sid)"
              @dblclick="row.item && startRename(row.item)"
            >
              <template v-if="renamingSid === row.item?.sid">
                <HlInput
                  v-model="renameText"
                  class="pilot-side__rename"
                  @keydown.enter="submitRename"
                  @keydown.esc="renamingSid = ''"
                />
                <button type="button" class="pilot-side__x is-ok" :title="t('pilot.history.rename')" @click.stop="submitRename">
                  <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                    <path d="M5 13l4 4L19 7" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" />
                  </svg>
                </button>
              </template>
              <template v-else>
                <span class="pilot-side__main">
                  <span class="pilot-side__name">
                    <span
                      v-if="row.item?.pinned"
                      class="pilot-side__pinmark"
                      :title="t('pilot.session.pinned')"
                      aria-hidden="true"
                    >
                      <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                        <path d="M12 17v4M9 3h6l-1 7 3 3H7l3-3-1-7Z" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" />
                      </svg>
                    </span>
                    <span
                      v-if="row.item?.running"
                      class="pilot-side__running"
                      :title="t('pilot.session.running')"
                      aria-hidden="true"
                    ></span>
                    <!-- 派生标记：窄列会省略标题尾部，故标记走标题前缀而非后缀 -->
                    <span
                      v-if="row.item?.forked_from"
                      class="pilot-forkmark"
                      :title="t('pilot.session.forked')"
                      aria-hidden="true"
                    >
                      <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                        <circle cx="6.5" cy="6" r="2.4" />
                        <circle cx="6.5" cy="18" r="2.4" />
                        <circle cx="17.5" cy="9.5" r="2.4" />
                        <path d="M6.5 8.4v7.2M8.9 6h4.1a4.5 4.5 0 0 1 4.5 1.4" />
                      </svg>
                    </span>
                    {{ row.item?.title || t('pilot.session.untitled') }}
                  </span>
                  <span class="pilot-side__meta">
                    {{ t('pilot.history.turns', { n: row.item?.turn_total ?? 0 }) }} · {{ sessionTime(row.item?.updated_at ?? null) }}
                  </span>
                  <span v-if="row.item?.snippet" class="pilot-side__snippet">{{ row.item.snippet }}</span>
                </span>
                <span class="pilot-side__acts">
                  <button
                    type="button"
                    class="pilot-side__x"
                    :class="{ 'is-on': row.item?.pinned }"
                    :title="row.item?.pinned ? t('pilot.session.unpin') : t('pilot.session.pin')"
                    @click.stop="row.item && togglePin(row.item)"
                  >
                    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                      <path d="M12 17v4M9 3h6l-1 7 3 3H7l3-3-1-7Z" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" />
                    </svg>
                  </button>
                  <button
                    type="button"
                    class="pilot-side__x"
                    :class="{ 'is-on': row.item?.archived }"
                    :title="row.item?.archived ? t('pilot.session.unarchive') : t('pilot.session.archive')"
                    @click.stop="row.item && toggleArchive(row.item)"
                  >
                    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                      <path d="M3 7h18M5 7v12a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V7M9 11h6" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" />
                    </svg>
                  </button>
                  <button
                    type="button"
                    class="pilot-side__x"
                    :class="{ 'is-armed': deleteArmSid === row.item?.sid }"
                    :title="deleteArmSid === row.item?.sid ? t('pilot.history.deleteConfirm') : t('pilot.history.delete')"
                    @click.stop="row.item && armDelete(row.item.sid)"
                  >
                    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                      <path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" />
                    </svg>
                  </button>
                </span>
              </template>
            </div>
          </template>
        </template>
      </div>
    </aside>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from '@/locales'
import type { PilotSessionListItem } from '@/api/client'
import { sessionTime } from '@/lib/pilotView'
import HlInput from '@/components/ui/HlInput.vue'
import HlPilotOutline from '@/components/business/HlPilotOutline.vue'
import HlPilotRuns from '@/components/business/HlPilotRuns.vue'
import { useSessionRowActions } from '@/composables/useSessionRowActions'

const props = defineProps<{
  /** 账本全量清单（null=未到，渲染空态不猜） */
  sessions: PilotSessionListItem[] | null
  sessionId: string
  streaming: boolean
  /** 当前会话尚未落账本（真新对话） */
  freshChat: boolean
  /** 会话标题派生（分叉前缀等，Console 统一口径） */
  labelOf: (item: Partial<PilotSessionListItem>) => string
  turns: unknown[]
  outlineCount: number
  outlineActive: number
}>()

const emit = defineEmits<{
  (e: 'new'): void
  (e: 'open', sid: string): void
  (e: 'search'): void
  (e: 'refresh'): void
  (e: 'deleted', sid: string): void
  (e: 'jump', index: number): void
  (e: 'outline-toggle'): void
}>()

const query = defineModel<string>({ default: '' })

const { t } = useI18n()

const { renamingSid, renameText, deleteArmSid, startRename, submitRename, armDelete, togglePin, toggleArchive } =
  useSessionRowActions({ sessions: () => props.sessions, refresh: () => emit('refresh'), deleted: (sid) => emit('deleted', sid) })

/* 检索输入：防抖 300ms 后交账本重投影（后端过滤，非前端筛） */
let searchTimer: ReturnType<typeof setTimeout> | null = null
function onQueryInput() {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    searchTimer = null
    emit('search')
  }, 300)
}

/** 会话列分区：归档项从主列收进折叠区；置顶由后端排序在前（前端不再重排）。
    检索态下不分区——命中结果平铺，避免归档命中被折叠区藏住。 */
const searching = computed(() => query.value.trim().length > 0)
const liveSessions = computed(() => {
  const all = props.sessions ?? []
  return searching.value ? all : all.filter((s) => !s.archived)
})
const archivedSessions = computed(() => (props.sessions ?? []).filter((s) => s.archived))
const archivedOpen = ref(false)

/** 会话列渲染行：主列 + （非检索态下的）归档折叠头 + 展开的归档项，扁平成一个数组 */
interface SideRow { head?: boolean; item?: PilotSessionListItem; count?: number }
const rows = computed<SideRow[]>(() => {
  const out: SideRow[] = liveSessions.value.map((it) => ({ item: it }))
  if (!searching.value && archivedSessions.value.length) {
    out.push({ head: true, count: archivedSessions.value.length })
    if (archivedOpen.value) for (const it of archivedSessions.value) out.push({ item: it })
  }
  return out
})
</script>

<style scoped>
.pilot-side {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.pilot-side__tools {
  display: flex;
  align-items: center;
  gap: 4px;
}

.pilot-side__new {
  flex: 1;
  min-width: 0;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px;
  border: 1px dashed var(--border-soft);
  border-radius: 8px;
  background: none;
  color: var(--text-secondary);
  font-size: 12px;
  cursor: pointer;
  white-space: nowrap;
}

.pilot-side__new:hover:not(:disabled),
.pilot-side__new.is-current {
  border-color: var(--accent-a30);
  background: var(--accent-a10);
  color: var(--accent);
}

.pilot-side__new:disabled {
  opacity: 0.4;
}

.pilot-side__new svg {
  width: 13px;
  height: 13px;
  flex: none;
}

.pilot-side__scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 2px;
  scrollbar-width: thin;
  scrollbar-color: var(--scroll-thumb) transparent;
}

.pilot-side__row {
  position: relative;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  border-radius: 8px;
  cursor: pointer;
}

.pilot-side__row:hover {
  background: var(--accent-a10);
}

.pilot-side__row.is-current {
  background: var(--accent-a10);
}

.pilot-side__row.is-current::before {
  content: '';
  position: absolute;
  left: 0;
  top: 6px;
  bottom: 6px;
  width: 2px;
  border-radius: 1px;
  background: var(--accent);
}

.pilot-side__main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.pilot-side__name {
  display: flex;
  align-items: center;
  gap: 5px;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  color: var(--text-primary);
}

.pilot-side__row.is-current .pilot-side__name {
  color: var(--accent);
}

.pilot-side__meta {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11px;
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
}

/* 生成中脉动点：accent 圆点呼吸（reduced-motion 降级为常亮） */
.pilot-side__running {
  flex: none;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent);
  animation: pilot-running-pulse 1.2s ease-in-out infinite;
}

@keyframes pilot-running-pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.3;
  }
}

@media (prefers-reduced-motion: reduce) {
  .pilot-side__running {
    animation: none;
  }
}

.pilot-side__x {
  flex: none;
  display: grid;
  place-items: center;
  width: 20px;
  height: 20px;
  padding: 0;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.15s;
}

.pilot-side__row:hover .pilot-side__x,
.pilot-side__x.is-armed,
.pilot-side__x.is-ok {
  opacity: 1;
}

.pilot-side__x:hover,
.pilot-side__x.is-armed {
  background: var(--accent-a10);
  color: var(--warning);
}

.pilot-side__x.is-ok {
  color: var(--success);
}

.pilot-side__x svg {
  width: 12px;
  height: 12px;
}

/* 已置顶/已归档动作钮：状态恒显（不靠 hover），用 accent 标「开着」 */
.pilot-side__x.is-on {
  opacity: 1;
  color: var(--accent);
}

/* 搜索框：常驻列头（固定不随列表滚动） */
.pilot-side__search {
  flex: none;
}

.pilot-side__search-box {
  width: 100%;
}

/* 归档折叠头：点开条，弱色不抢主列注意力 */
.pilot-side__arch {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 5px 8px;
  border: none;
  background: none;
  color: var(--text-dim);
  font-size: 11px;
  text-align: left;
  cursor: pointer;
}

.pilot-side__arch:hover {
  color: var(--text-secondary);
}

.pilot-side__arch-ico {
  width: 11px;
  height: 11px;
  flex: none;
  transition: transform 0.15s;
}

.pilot-side__arch-ico.is-open {
  transform: rotate(90deg);
}

/* 归档项在主列之外再压一档：名与元信息降色 */
.pilot-side__row.is-arch .pilot-side__name {
  color: var(--text-secondary);
}

/* 置顶标记：小图钉恒显（不是 hover 才出的动作钮） */
.pilot-side__pinmark {
  flex: none;
  display: grid;
  place-items: center;
  width: 11px;
  height: 11px;
  color: var(--accent);
}

.pilot-side__pinmark svg {
  width: 11px;
  height: 11px;
}

/* 派生标记：标题前缀的常驻小图标（标签栏 / 历史面板同类） */
.pilot-forkmark {
  flex: none;
  display: inline-grid;
  place-items: center;
  width: 11px;
  height: 11px;
  margin-right: 4px;
  vertical-align: -1px;
  color: var(--text-muted);
}

.pilot-forkmark svg {
  width: 11px;
  height: 11px;
  stroke: currentColor;
}

/* 检索命中的正文片段：单行省略（「…」由后端裁好） */
.pilot-side__snippet {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11px;
  color: var(--text-dim);
}

/* 行内动作组：置顶/归档/删除三钮并排（占位恒定，hover 才显形） */
.pilot-side__acts {
  flex: none;
  display: flex;
  align-items: center;
  gap: 2px;
}

.pilot-side__rename {
  flex: 1;
  min-width: 0;
}

.pilot-side__empty {
  padding: 12px 8px;
  font-size: 12px;
  color: var(--text-secondary);
  text-align: center;
}
</style>
