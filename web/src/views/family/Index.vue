<script setup lang="ts">
import { computed, onMounted, ref, type Component } from 'vue'
import { flagUrl } from '@/api/regions'
import { useRegionsStore } from '@/stores/regions'
import { useFamilyStore } from '@/stores/familyLib'
import { familyApi, watchPoolApi, type FamilyMemberItem } from '@/api/client'
import { normalizeAvatarUrl } from '@/api/avatar'
import { useI18n, type MessageKey } from '@/locales'
import { HlPaneSwitch, HlTabs, message } from '@/components/ui'
import type { HlSelectOption, HlTabItem } from '@/components/ui'
import FamContrib from './tabs/FamContrib.vue'
// 增长趋势暂时隐藏：与入库热力图信息重叠，代码保留待回归
// import FamGrowth from './tabs/FamGrowth.vue'
import FamHeat from './tabs/FamHeat.vue'
import FamBuy from './tabs/FamBuy.vue'
import FamWish from './tabs/FamWish.vue'
// 游玩动态在游戏库页（views/gamelib/tabs/GlPlay.vue，词条 famPlay.* 随迁）

const regionsStore = useRegionsStore()
const libStore = useFamilyStore()
const { t } = useI18n()

interface FamilyMember {
  id: number
  steamid: string
  name: string
  avatar: string
  avatarUrl: string
  role: 'primary' | 'family'
  friendCode: string
  region: string
  contribution: number
  exclusive: number
  recent30: number
}

const members = ref<FamilyMember[]>([])
const familyName = ref('')
const familyBound = ref(false)
const familyMessage = ref('')
/** 家庭状态是否已成功取回。用于 onMounted 去重：本页每次进入都会重挂载，
 *  没有这个标记就会重打一次 /family/status（见下方 onMounted 的注释）。
 *  只在**成功**后置位——失败时留待下次进入重试。 */
const familyLoaded = ref(false)

/* ── 成员真实统计（familyLib store：独占贡献/近30日入库/库价值）──
   近30日 = 入库口径（timeAcquired，入库口径取 timeAcquired——lastPlayed 是游玩不是入库） */
const memberRealStats = computed(() => {
  const map = new Map<string, { exclusive: number; recent30: number; owned: number }>()
  const nowSec = Math.floor(Date.now() / 1000)
  for (const st of libStore.memberStats) {
    const recent30 = libStore.games.filter(
      (g) => g.owners.includes(st.steamid) && g.timeAcquired > nowSec - 30 * 86400,
    ).length
    map.set(st.steamid, { exclusive: st.exclusiveCount, recent30, owned: st.ownedCount })
  }
  return map
})

function statsOf(m: FamilyMember) {
  return memberRealStats.value.get(m.steamid) ?? { exclusive: 0, recent30: 0, owned: 0 }
}

/** 成员显示名：后端没给昵称时用「成员1234」兜底。
 *  兜底串**不能在 loadFamily 里 t()**——那是把语言冻在数据加载那一刻：译文写进
 *  members 状态后只算一次，切语言不会重算。故 name 只存后端原始值（可空），
 *  渲染期由本函数现取。 */
function memberName(m: FamilyMember) {
  return m.name || t('family.member.unnamed', { id: m.steamid.slice(-4) })
}

/** 角色短徽章（D8：徽章用短 Family，与 bundles.badge.family 一致） */
function roleBadge(m: FamilyMember) {
  return m.role === 'primary' ? t('family.role.primary') : t('family.role.family')
}

/** 角色叙述（D8：二级窗用 family member；与徽章的长短分工是有意的，不要合并） */
function roleNarrative(m: FamilyMember) {
  return m.role === 'primary' ? t('family.role.primary') : t('family.role.familyMember')
}

/* ── 真数据源：GET /family/status（家庭组快照）── */
async function loadFamily() {
  try {
    const st = await familyApi.status()
    familyBound.value = st.bound
    familyMessage.value = st.message || ''
    familyName.value = st.familyName || ''
    // 主账号 Cookie 钱包派生的结算地区（币种反查区服，如 INR→印度）
    const walletRegion = st.walletRegion || ''
    const savedRegions = st.memberRegions || {}
    if (st.members?.length) {
      members.value = st.members.map((m: FamilyMemberItem, i: number) => {
        // 地区优先级：钱包判定（仅 Cookie 归属主账号）> 手动选择持久化 > 默认 cn。
        // 注意 Steam role 1=成年 2=儿童（全家多人都是 1），主账号唯一判定 =
        // status.steamid（Cookie 归属账户），不能用 role。
        const manual = savedRegions[m.steamid]
        const isPrimary = (st.steamid && m.steamid === st.steamid) || i === 0
        let region = manual || 'cn'
        if (isPrimary && walletRegion) region = walletRegion
        return {
          id: i + 1,
          steamid: m.steamid,
          name: m.personaName || '',
          avatar: '',
          avatarUrl: normalizeAvatarUrl(m.avatarUrl),
          role: isPrimary ? 'primary' : 'family',
          friendCode: String(BigInt(m.steamid) - 76561197960265728n),
          region,
          contribution: 0,
          exclusive: 0,
          recent30: 0,
        }
      })
      normalizeRoles()
      // 主账号按钱包结算区自动对齐后，同步落库（保持 memberRegions 一致）
      if (walletRegion && members.value[0]?.region === walletRegion) {
        void persistRegions()
      }
    }
    // 只在成功路径置位：失败时留 false，下次进入本页会重试
    familyLoaded.value = true
  } catch (e) {
    familyMessage.value = e instanceof Error ? e.message : String(e)
  }
}

/* ── 同步家庭组：主账号 Cookie → GetFamilyGroupForUser → 成员自动补齐 ── */
const syncing = ref(false)
async function syncFamily() {
  syncing.value = true
  message.loading(t('family.action.syncing'))
  try {
    const r = await familyApi.sync()
    if (r.joined) {
      const groupName = r.familyName || t('family.group.unnamed')
      message.success(t('family.sync.success', { name: groupName, n: r.members.length }))
      void libStore.load(true)  // 家庭组变了：强制重拉家庭库聚合
    } else {
      message.info(r.message || t('family.sync.notJoined'))
    }
    await loadFamily()
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    syncing.value = false
  }
}

/* ── 添加成员输入框：好友码/SteamID64 实时解析预览 ── */
const addInput = ref('')
const resolving = ref(false)
const resolvePreview = ref<{ steamid: string; personaName: string; avatarUrl: string } | null>(null)
let resolveSeq = 0

/** 输入即解析（300ms 防抖）：好友码→SteamID 转换 + 实时拉昵称/头像确认身份 */
async function previewResolve() {
  const input = addInput.value.trim()
  const seq = ++resolveSeq
  resolvePreview.value = null
  if (!input) return
  resolving.value = true
  try {
    const r = await familyApi.resolve(input)
    if (seq === resolveSeq) {
      resolvePreview.value = { ...r, avatarUrl: normalizeAvatarUrl(r.avatarUrl) }
    }
  } catch (e) {
    // 输入未完成时解析失败静默（防抖中继续输入即可）
  } finally {
    if (seq === resolveSeq) resolving.value = false
  }
}

function onAddInput(e: Event) {
  const v = (e.target as HTMLInputElement).value
  addInput.value = v
  clearTimeout((onAddInput as unknown as { _t?: number })._t)
  ;(onAddInput as unknown as { _t?: number })._t = window.setTimeout(previewResolve, 300)
}

/** 确认添加：解析结果入库追踪（tracked_accounts）+ 加入家庭页列表；
    家庭组其余真实成员由「同步家庭组」按主账号 Cookie 自动补齐 */
const confirming = ref(false)
async function confirmAddMember() {
  const p = resolvePreview.value
  if (!p) {
    message.warning(t('family.add.needInput'))
    return
  }
  confirming.value = true
  try {
    // 入库追踪：进 tracked_accounts（愿望单/已购同步链路随之生效，无需等待家庭组）
    try {
      await watchPoolApi.add(p.steamid, p.personaName || '', { wishlist: true, owned: true })
    } catch (e) {
      // 重复添加（账户已存在）静默放行
      const msg = e instanceof Error ? e.message : String(e)
      if (!/已存在|exist/i.test(msg)) message.warning(t('family.add.trackFailed', { err: msg }))
    }
    const nextId = Math.max(...members.value.map((m) => m.id), 0) + 1
    members.value.push({
      id: nextId,
      steamid: p.steamid,
      name: p.personaName || '',
      avatar: '',
      avatarUrl: normalizeAvatarUrl(p.avatarUrl),
      role: 'family',
      friendCode: String(BigInt(p.steamid) - 76561197960265728n),
      region: 'cn',
      contribution: 0,
      exclusive: 0,
      recent30: 0,
    })
    normalizeRoles()
    addInput.value = ''
    resolvePreview.value = null
    message.success(t('family.add.success', { name: p.personaName || p.steamid }))
  } finally {
    confirming.value = false
  }
}

const activeTab = ref('contrib')

/* 页签常量表**只存词条 key**：模块级 const 存译文会把语言冻在模块加载那一刻
   （brief 的冻结陷阱），渲染期 `t(tab.labelKey)` 才是响应式的。`key` 是筛选用的
   代码，与语言无关，不参与翻译。 */
/** 页签的**唯一**登记处：key + 词条 key。渲染用的 `{ key, label }` 由下面的
 *  `tabItems` 每次渲染现取——模块级常量存译文会把语言冻在模块加载那一刻。 */
const TAB_KEYS: { key: string; labelKey: MessageKey }[] = [
  { key: 'contrib', labelKey: 'family.tab.contrib' },
  { key: 'heat', labelKey: 'family.tab.heat' },
  { key: 'buy', labelKey: 'family.tab.buy' },
  { key: 'wish', labelKey: 'family.tab.wish' },
  // 游玩动态与家庭库两页签在游戏库页（views/gamelib/tabs/GlPlay.vue、
  // views/gamelib/tabs/FamilyLib.vue），词条 famPlay.* / famLib.* 随之。
  // 增长趋势与共享冷却为停用实现（语义待定），代码保留但不出现在 tab 列表。
]

const tabItems = computed<HlTabItem[]>(() =>
  TAB_KEYS.map((x) => ({ key: x.key, label: t(x.labelKey) })),
)

/* 页签 → 面板组件。**为什么用映射而不是 v-if 链**：面板切换要过渡，而
   `<Transition>` 只接受单个子节点——v-if 链会编译成一个 fragment，Vue 会告警
   且过渡根本不触发（此前 `display:none → block` 的硬切就是这么来的）。
   映射本身也是页签清单的**唯一**第二处登记：加面板要同时改这里与上面的 TAB_KEYS。 */
const PANES: Record<string, Component> = {
  contrib: FamContrib,
  heat: FamHeat,
  buy: FamBuy,
  wish: FamWish,
  // growth: FamGrowth,  // 停用（与热力图信息重叠），回归时与上面的 import、tabs 一起放开
  // cool: …,           // 停用（语义待研究），回归时见模板内保留的块
}

/**
 * 第一行永远视作本账号：拖拽/增删后实时归位角色，
 * 幕布色条（fam-row--primary 绿 / --family 紫）随位置即时刷新。
 */
function normalizeRoles() {
  members.value.forEach((m, i) => {
    m.role = i === 0 ? 'primary' : 'family'
  })
}

/** 成员地区二级窗（框架标准：国徽旗 + 幕布选中态） */
const memberRegionOptions = computed<HlSelectOption[]>(() =>
  regionsStore.list.map((r) => ({
    label: r.name,
    value: r.code.toLowerCase(),
    flag: flagUrl(r.code),
  })),
)

/* ── 拖拽排序（修复：此前仅样式有 cursor:grab，未实现拖拽逻辑）── */
const dragIndex = ref<number | null>(null)
const dragOverIndex = ref<number | null>(null)

function onDragStart(i: number) {
  dragIndex.value = i
}
function onDragEnter(i: number) {
  if (dragIndex.value !== null) dragOverIndex.value = i
}
function onDrop(i: number) {
  const from = dragIndex.value
  if (from !== null && from !== i) {
    const next = [...members.value]
    const [moved] = next.splice(from, 1)
    next.splice(i, 0, moved!)
    members.value = next
  }
  dragIndex.value = null
  dragOverIndex.value = null
  normalizeRoles()
}
function onDragEnd() {
  dragIndex.value = null
  dragOverIndex.value = null
}

/* ── 地区选择二级窗：头部=正在修改的成员；搜索实时过滤；网格随宽度自适应 ── */
const popMember = ref<FamilyMember | null>(null)
const regionSearch = ref('')
const popStyle = ref<{ top: string; left: string; width: string } | null>(null)
const fxModuleRef = ref<HTMLElement | null>(null)

const filteredRegionOptions = computed(() => {
  const kw = regionSearch.value.trim().toLowerCase()
  if (!kw) return memberRegionOptions.value
  return memberRegionOptions.value.filter(
    (o) =>
      o.label.toLowerCase().includes(kw) ||
      String(o.value).toLowerCase().includes(kw),
  )
})

function openRegionPop(m: FamilyMember, e: MouseEvent) {
  if (popMember.value?.id === m.id) {
    popMember.value = null
    return
  }
  popMember.value = m
  regionSearch.value = ''
  const btn = (e.currentTarget as HTMLElement).getBoundingClientRect()
  const mod = fxModuleRef.value?.getBoundingClientRect()
  if (mod) {
    const width = Math.max(280, mod.width - 32)
    popStyle.value = {
      top: `${btn.bottom - mod.top + 8}px`,
      left: '16px',
      width: `${width}px`,
    }
  }
}

function closeRegionPop() {
  popMember.value = null
  regionSearch.value = ''
}

function chooseRegion(m: FamilyMember, code: string | number) {
  m.region = String(code)
  closeRegionPop()
  void persistRegions()
}

/** 成员地区落库（手动切换 / 主账号钱包自动对齐后调用；失败静默不扰交互） */
async function persistRegions() {
  try {
    await familyApi.saveMemberRegions(
      Object.fromEntries(members.value.map((m) => [m.steamid, m.region])),
    )
  } catch {
    /* 静默：下次切换重试 */
  }
}

function regionName(code: string) {
  const r = regionsStore.list.find((r) => r.code.toLowerCase() === code.toLowerCase())
  return r ? r.name : code.toUpperCase()
}

function removeMember(id: number) {
  members.value = members.value.filter((m) => m.id !== id)
  normalizeRoles()
}

onMounted(() => {
  // 本组件不带 KeepAlive，板块来回切就会重挂载一次。两个请求都自带复用守卫：
  // /family/status 靠 familyLoaded，家庭库聚合靠 store 内的 ready（force=true 仍可穿透）。
  if (!familyLoaded.value) void loadFamily()
  void libStore.load()  // 家庭库聚合（tabs/成员统计共用；未绑 Cookie 时各 tab 诚实空态）
})

/* 首字母头像的两条渐变。走令牌以跟随主题：
   --accent-fill → --accent 是「浅一档 → 本色」，--purple → --purple-deep 同理。 */
const avatarColors: Record<string, string> = {
  primary: 'linear-gradient(135deg, var(--accent-fill), var(--accent))',
  family: 'linear-gradient(135deg, var(--purple), var(--purple-deep))',
}

/* ─ 贡献分布（堆叠条形+环形+近半年增量+范围切换）在独立组件 FamContrib.vue ── */

/* ── 共享冷却（真数据：GetSharedLibraryApps 排除清单）——tab 未启用（语义待定），代码保留 ─
const shareableCount = computed(() => libStore.games.filter((g) => g.inSharedLib && !g.excluded).length)
const excludedCount = computed(() => libStore.games.filter((g) => g.excluded).length)
const excludedBySteamid = computed(() => {
  // 后端 exclude 数据粒度到 app；按拥有者归组（近似到成员）
  const map = new Map<string, number[]>()
  for (const g of libStore.games) {
    if (!g.excluded) continue
    const sid = g.owners[0] ?? 'unknown'
    if (!map.has(sid)) map.set(sid, [])
    map.get(sid)!.push(g.appid)
  }
  return map.size ? map : null
})
*/
</script>

<template>
  <section class="family-page">
    <div ref="fxModuleRef" class="fx-module" style="position: relative" data-section="family.section.module">
      <div class="fx-module__head">
        <div class="fx-module__icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/></svg>
        </div>
        <div>
          <div class="fx-module__title">{{ t('family.section.module') }}</div>
          <div class="fx-module__sub">{{ t('family.module.sub') }}</div>
        </div>
      </div>

      <!-- 成员列表（对齐好友码列表 UI；拖拽排序） -->
      <div class="fam-list">
        <div
          v-for="(m, i) in members"
          :key="m.id"
          class="fam-row"
          :class="{
            'fam-row--primary': m.role === 'primary',
            'fam-row--family': m.role === 'family',
            'is-me': m.role === 'primary',
            'is-dragging': dragIndex === i,
            'is-dragover': dragOverIndex === i && dragIndex !== null && dragIndex !== i,
          }"
          draggable="true"
          @dragstart="onDragStart(i)"
          @dragenter.prevent="onDragEnter(i)"
          @dragover.prevent
          @drop.prevent="onDrop(i)"
          @dragend="onDragEnd"
        >
          <div class="fam-row__ava" :style="{ background: avatarColors[m.role] }">
            <HlImg :src="m.avatarUrl" :alt="memberName(m)" loading="lazy">
              <template #fallback>
                <template>{{ (memberName(m) || '?').slice(0, 1) }}</template>
              </template>
            </HlImg>
          </div>
          <div class="fam-row__main">
            <div class="fam-row__name">
              {{ memberName(m) }}
              <span class="fam-role" :class="m.role === 'primary' ? 'fam-role--primary' : 'fam-role--family'">
                {{ roleBadge(m) }}
              </span>
            </div>
            <div class="fam-row__sub">
              <span>{{ t('family.member.friendCode', { code: m.friendCode || t('family.member.notLinked') }) }}</span>
              <em>{{ t('family.member.owned', { n: statsOf(m).owned }) }}</em>
              <em>{{ t('family.member.exclusive', { n: statsOf(m).exclusive }) }}</em>
              <em>{{ t('family.member.recent30', { n: statsOf(m).recent30 > 0 ? '+' + statsOf(m).recent30 : statsOf(m).recent30 }) }}</em>
            </div>
          </div>
          <div class="fam-row__side">
            <!-- 地区选择触发钮（二级窗见下方） -->
            <button
              type="button"
              class="fam-region-btn"
              :class="{ 'is-open': popMember?.id === m.id }"
              @click.stop="openRegionPop(m, $event)"
            >
              <img :src="flagUrl(m.region)" :alt="m.region" />
              {{ regionName(m.region) }}
              <span class="car">▼</span>
            </button>
            <span class="fam-del" :title="t('family.member.remove')" @click="removeMember(m.id)">✕</span>
          </div>
        </div>
      </div>

      <!-- 地区选择二级窗：宽度 ≤ 模块宽；头部=正在修改的成员；搜索实时过滤；网格自适应 -->
      <teleport to="body">
        <div
          v-if="popMember && popStyle"
          class="fam-pop-mask"
          @mousedown="closeRegionPop"
        />
      </teleport>
      <div
        v-if="popMember && popStyle"
        class="fam-region-pop"
        :style="popStyle"
        @mousedown.stop
      >
        <div class="fam-region-pop__head">
          <div class="fam-row__ava" :style="{ background: avatarColors[popMember.role] }">
            {{ popMember.avatar }}
          </div>
          <div class="fam-region-pop__who">
            <div class="fam-region-pop__name">{{ memberName(popMember) }}</div>
            <div class="fam-region-pop__sub">
              {{ t('family.regionPop.who', { role: roleNarrative(popMember), region: regionName(popMember.region) }) }}
            </div>
          </div>
        </div>
        <input
          v-model="regionSearch"
          type="text"
          class="fam-region-pop__search"
          :placeholder="t('family.regionPop.search')"
        />
        <div class="fam-region-pop__grid">
          <button
            v-for="opt in filteredRegionOptions"
            :key="opt.value"
            type="button"
            class="fam-region-pop__item"
            :class="{ 'is-active': popMember.region === opt.value }"
            @click="chooseRegion(popMember, opt.value!)"
          >
            <img :src="opt.flag" :alt="opt.label" />
            {{ opt.label }}
          </button>
          <div v-if="filteredRegionOptions.length === 0" class="fam-region-pop__empty">
            {{ t('family.regionPop.empty') }}
          </div>
        </div>
      </div>

      <!-- 未绑 Cookie / 未同步的引导横幅 -->
      <div v-if="familyMessage" class="fam-hint">{{ familyMessage }}</div>

      <!-- 同步家庭组 + 添加成员（真数据链路入口） -->
      <div class="fam-add-row">
        <button class="fam-sync" :disabled="syncing" :aria-busy="syncing || undefined" @click="syncFamily">
          <span v-if="syncing" class="hl-spinner hl-spinner--inline" aria-hidden="true" />
          {{ syncing ? t('family.action.syncing') : t('family.action.sync') }}
        </button>
        <span v-if="familyName" class="fam-group-tag">{{ familyName }}</span>
        <input
          class="pxinput fam-add-input"
          :placeholder="t('family.add.placeholder')"
          :value="addInput"
          @input="onAddInput"
          @keyup.enter="confirmAddMember"
        />
        <button class="fam-sync" :disabled="!resolvePreview || resolving || confirming" @click="confirmAddMember">
          {{ confirming ? t('family.add.confirming') : t('family.add.action') }}
        </button>
      </div>
      <!-- 实时解析预览：好友码→SteamID + 真实昵称/头像 -->
      <div v-if="resolving || resolvePreview" class="fam-resolve-preview">
        <template v-if="resolvePreview">
          <HlImg :src="resolvePreview.avatarUrl" alt="" />
          <span class="fam-resolve-preview__name">
            {{ resolvePreview.personaName || t('family.resolve.noName') }}
          </span>
          <span class="fam-resolve-preview__sid mono">
            {{ resolvePreview.steamid }}
          </span>
          <span class="tag">{{ t('family.resolve.ok') }}</span>
        </template>
        <span v-else class="fam-resolve-preview__loading">{{ t('family.resolve.loading') }}</span>
      </div>

      <button class="fam-add" @click="confirmAddMember">
        {{ t('family.add.invite', { n: members.length, left: 6 - members.length }) }}
      </button>

      <!-- 标签卡。页签栏此前是本文件手写的 `.tabs__bar` / `.tabs__item`（用 span 点击），
           与 HlTabs 是同一件东西，已改回组件。 -->
      <HlTabs v-model="activeTab" :tabs="tabItems" class="fam-tabs">
        <!-- 面板切换统一走 HlPaneSwitch。**为什么不是 v-if 链**：页签切换要过渡，
             而 <Transition> 只接受单个子节点，v-if 链会编译成 fragment，Vue 会
             告警且过渡根本不生效（这正是此前「切页签是硬切」的成因之一）。
             页面级过渡还有一个坑见 HlPaneSwitch 的注：过渡类必须带显式时长，
             否则后台标签页里 rAF 不跑，mode="out-in" 会把新面板**永久卡住**。 -->
        <HlPaneSwitch :pane-key="activeTab">
          <component :is="PANES[activeTab]" />
        </HlPaneSwitch>

          <!-- ── 以下两块为**停用实现**，代码原样保留待回归；回归时注意两点 ──
               ① 面板要加进上面的 PANES 映射与 tabs 数组，不要改回 v-if 链；
               ② 那三张统计卡改用 <HlStat>，其原 .fx-mini-stat 样式已随统一收敛删除。 -->

          <!-- 增长趋势（月度入库累计折线）——暂时隐藏（与热力图信息重叠），代码保留待回归
          <FamGrowth />
          增长趋势隐藏块结束 -->

          <!-- 共享冷却（真实数据：被 Steam 排除出共享库的成员/游戏）——tab 暂时隐藏（语义待研究），代码保留待回归。
               回归时补回最外层 `<div v-if="activeTab === 'cool'" class="tabs__pane is-on">`（或把整块做成 cool 的 PANES 条目），
               下方 `</template>` 之后的收尾已对齐。
          <div v-if="libStore.error || (!libStore.ready && libStore.loading)" class="lib-empty-tab">
          {{ libStore.error || '家庭库数据拉取中…' }}
          </div>
          <div v-else-if="libStore.games.length === 0" class="lib-empty-tab">
          暂无家庭库数据——同步家庭组后展示共享冷却状态
          </div>
          <template v-else>
          <div class="fx-grid--3" style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px">
          <div class="fx-mini-stat">
          <div class="fx-mini-stat__num fx-mini-stat__num--good">{{ shareableCount }}</div>
          <div class="fx-mini-stat__lbl">共享库内游戏（正常共享）</div>
          </div>
          <div class="fx-mini-stat">
          <div class="fx-mini-stat__num fx-mini-stat__num--warn">{{ excludedCount }}</div>
          <div class="fx-mini-stat__lbl">被排除出共享库（Steam 侧限制）</div>
          </div>
          <div class="fx-mini-stat">
          <div class="fx-mini-stat__num">{{ excludedBySteamid ? excludedBySteamid.size : 0 }}</div>
          <div class="fx-mini-stat__lbl">涉及成员数</div>
          </div>
          </div>
          <div v-if="excludedCount > 0" style="margin-top: 10px; font-size: 11.5px; color: var(--text-muted); line-height: 1.8; background: var(--surface-inset); border-radius: var(--radius); padding: 10px 13px">
          <b style="color: var(--warning)">排除清单：</b>
          <span v-for="(appids, sid) in excludedBySteamid" :key="sid">
          {{ libStore.memberName(String(sid)) }}（{{ appids.length }} 款：{{ appids.slice(0, 5).map((a) => libStore.games.find((g) => g.appid === a)?.name || a).join('、') }}{{ appids.length > 5 ? '…' : '' }}）；
          </span>
          </div>
          <div style="margin-top: 12px; font-size: 11.5px; color: var(--text-muted); line-height: 1.8; background: var(--surface-inset); border-radius: var(--radius); padding: 10px 13px">
          Steam 家庭共享冷却机制：成员退出家庭组后需等待 1 年方可重新加入；跨区账号可能触发地区冷却。
          冷却期内成员无法享受家庭库共享权益。Steam Web API 不开放冷却剩余时间，此处按共享库排除清单实时呈现。
          </div>
          </template>
共享冷却隐藏块结束 -->
      </HlTabs>
    </div>
  </section>
</template>

<style scoped>
.family-page {
  max-width: 1000px;
  margin: 0 auto;
}

/* 未来模块容器 */
.fx-module {
  background: var(--bg-card);
  border: 1px solid var(--line-1);
  border-radius: var(--radius-lg);
  padding: 18px;
  box-shadow: var(--shadow-sm);
}
.fx-module__head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 4px;
  flex-wrap: wrap;
}
.fx-module__icon {
  width: 34px;
  height: 34px;
  border-radius: var(--radius);
  display: grid;
  place-items: center;
  background: var(--accent-a15);
  color: var(--accent);
  flex-shrink: 0;
}
.fx-module__icon svg { width: 19px; height: 19px; }
.fx-module__title { font-size: 15px; font-weight: 700; color: var(--text-primary); }
.fx-module__sub { font-size: 12px; color: var(--text-muted); margin: 2px 0 14px 44px; }
.fx-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 14px; }
.fx-grid--3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
@media (max-width: 1100px) { .fx-grid, .fx-grid--3 { grid-template-columns: 1fr; } }
.fx-empty-mini {
  padding: 26px 20px;
  text-align: center;
  font-size: 12.5px;
  color: var(--text-dim);
  background: var(--surface-inset);
  border: 1px dashed var(--border-soft);
  border-radius: var(--radius);
  line-height: 1.8;
}

/* 统计卡：本文件已无活引用（唯一使用处是上面「共享冷却」停用块，它用的是类名
   而不是这套规则）。卡片本体统一到 components/ui/HlStat.vue，故删除。
   ⚠️ 回归「共享冷却」时把那三张卡改写成 <HlStat> 即可，别再恢复本块样式。 */

/* 比例条 */
.ratio-row { display: flex; align-items: center; gap: 9px; margin-bottom: 9px; font-size: 12px; }
.ratio-row__name { width: 64px; color: var(--text-secondary); flex-shrink: 0; }
.ratio-row__track { flex: 1; height: 8px; border-radius: 999px; background: var(--surface-track); overflow: hidden; }
.ratio-row__fill { height: 100%; border-radius: 999px; transition: width 0.9s var(--ease-out); }
.ratio-row__val { width: 52px; text-align: right; font-family: var(--font-mono); color: var(--text-muted); font-size: 11px; flex-shrink: 0; }

/* 家庭成员行卡 */
.fam-list {
  border: 1px solid var(--line-1);
  border-radius: var(--radius);
  overflow: hidden;
  background: var(--surface-inset);
}
.fam-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--row-border);
  border-left: 3px solid transparent;
  transition: background 0.15s;
  position: relative;
}
.fam-row:last-of-type { border-bottom: none; }
.fam-row--primary {
  background: color-mix(in srgb, var(--success) 12%, transparent);
  border-left-color: var(--success);
}
.fam-row--family {
  background: color-mix(in srgb, #9b59b6 10%, transparent);
  border-left-color: #9b59b6;
}
.fam-row.is-me {
  box-shadow: inset 0 0 0 1.5px var(--accent);
}
.fam-row__ava {
  width: 30px;
  height: 30px;
  border-radius: 6px;
  object-fit: cover;
  flex-shrink: 0;
  display: grid;
  place-items: center;
  color: var(--text-on-fill);
  font-weight: 700;
  font-size: 13px;
  overflow: hidden;
}
.fam-row__ava img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}
.fam-row__main { flex: 1; min-width: 0; }
.fam-row__name {
  font-size: 13px;
  font-weight: 600;
  color: var(--accent);
  display: flex;
  align-items: center;
  gap: 7px;
}
.fam-role {
  font-size: 9.5px;
  padding: 1px 6px;
  border-radius: 3px;
  font-weight: 700;
  letter-spacing: 0.3px;
}
.fam-role--primary {
  background: color-mix(in srgb, var(--success) 28%, transparent);
  color: var(--success);
}
.fam-role--family {
  background: color-mix(in srgb, #9b59b6 30%, transparent);
  color: #c39bd3;
}
.fam-row__sub {
  font-size: 10.5px;
  color: var(--text-dim);
  font-family: var(--font-mono);
  margin-top: 2px;
  display: flex;
  gap: 9px;
  flex-wrap: wrap;
}
.fam-row__sub em { font-style: normal; color: var(--text-muted); }
.fam-row__side { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }

.fam-del {
  color: var(--danger);
  cursor: pointer;
  font-size: 13px;
  width: 22px;
  height: 22px;
  display: grid;
  place-items: center;
  border-radius: 5px;
  opacity: 0.6;
  transition: var(--transition);
}
.fam-del:hover {
  opacity: 1;
  background: var(--danger-a15);
}
.fam-add {
  width: 100%;
  padding: 9px;
  border: 1px dashed var(--border-strong);
  border-radius: var(--radius);
  background: transparent;
  color: var(--text-muted);
  font-size: 12.5px;
  cursor: pointer;
  margin-top: 9px;
  transition: var(--transition);
}
.fam-add:hover {
  border-color: var(--accent);
  color: var(--accent);
  background: var(--accent-a08);
}

/* 同步/添加操作行 */
.fam-add-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 9px;
  flex-wrap: wrap;
}
.fam-sync {
  padding: 7px 12px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius);
  background: var(--accent-a08);
  color: var(--accent);
  font-size: 12.5px;
  font-weight: 600;
  cursor: pointer;
  transition: var(--transition);
  white-space: nowrap;
}
.fam-sync:hover:not(:disabled) {
  border-color: var(--accent);
  background: var(--accent-a15);
}
.fam-sync:disabled {
  opacity: 0.55;
  cursor: default;
}
.fam-group-tag {
  padding: 4px 8px;
  border-radius: var(--radius);
  background: var(--purple-a15, rgba(154, 106, 220, 0.15));
  color: var(--purple, #9a6adc);
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
}
.fam-add-input {
  flex: 1;
  min-width: 220px;
  max-width: 420px;
}
.fam-resolve-preview {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
  padding: 8px 10px;
  border: 1px solid var(--border-soft);
  border-radius: var(--radius);
  background: var(--surface-inset, transparent);
  font-size: 12.5px;
}
.fam-resolve-preview img {
  width: 26px;
  height: 26px;
  border-radius: 6px;
  object-fit: cover;
}
.fam-resolve-preview__name {
  color: var(--text-primary);
  font-weight: 600;
}
.fam-resolve-preview__sid {
  color: var(--text-muted);
  font-size: 12px;
}
.fam-resolve-preview__loading {
  color: var(--text-muted);
}
.fam-hint {
  margin-top: 10px;
  padding: 8px 12px;
  border: 1px dashed var(--border-strong);
  border-radius: var(--radius);
  color: var(--text-muted);
  font-size: 12.5px;
}

/* 标签卡 */
.fam-tabs { margin-top: 12px; }

/* ── 拖拽排序 ── */
.fam-row[draggable] { cursor: grab; }
.fam-row[draggable]:active { cursor: grabbing; }
.fam-row.is-dragging { opacity: 0.4; }
.fam-row.is-dragover { box-shadow: inset 0 2px 0 var(--accent); }

/* ── 地区触发钮 ── */
.fam-region-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11.5px;
  padding: 4px 9px;
  border-radius: 6px;
  border: 1px solid var(--border-soft);
  background: var(--bg-card);
  color: var(--text-secondary);
  cursor: pointer;
  transition: var(--transition);
  font-family: inherit;
}
.fam-region-btn img { width: 16px; height: 12px; border-radius: 2px; object-fit: cover; }
.fam-region-btn:hover,
.fam-region-btn.is-open { border-color: var(--accent); color: var(--accent); }
.fam-region-btn .car { font-size: 9px; color: var(--text-muted); }

/* ── 地区选择二级窗 ── */
.fam-pop-mask { position: fixed; inset: 0; z-index: 90; }
.fam-region-pop {
  position: absolute;
  z-index: 95;
  background: var(--bg-card);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  padding: 14px;
}
.fam-region-pop__head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--line-1);
  margin-bottom: 10px;
}
.fam-region-pop__who { min-width: 0; }
.fam-region-pop__name { font-size: 13.5px; font-weight: 700; color: var(--text-primary); }
.fam-region-pop__sub { font-size: 11px; color: var(--text-muted); margin-top: 2px; }
.fam-region-pop__search {
  width: 100%;
  height: 34px;
  background: var(--input-bg);
  border: 1px solid var(--border-soft);
  border-radius: var(--radius);
  padding: 0 12px;
  font: inherit;
  font-size: 13px;
  color: var(--text-primary);
  outline: none;
  transition: box-shadow var(--transition);
}
.fam-region-pop__search::placeholder { color: var(--text-faint); }
.fam-region-pop__search:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-a15); }
.fam-region-pop__grid {
  margin-top: 10px;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(132px, 1fr));
  gap: 6px;
  max-height: 260px;
  overflow: auto;
}
.fam-region-pop__item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 9px;
  border-radius: 6px;
  border: 1px solid var(--border-soft);
  background: var(--bg-card);
  color: var(--text-secondary);
  font-size: 12px;
  cursor: pointer;
  transition: all var(--transition);
  text-align: left;
  font-family: inherit;
}
.fam-region-pop__item img { width: 18px; height: 13px; border-radius: 2px; object-fit: cover; }
.fam-region-pop__item:hover { border-color: var(--accent-a40); background: var(--hover-soft); }
/* 幕布选中态（全局标准 --select-selected-*） */
.fam-region-pop__item.is-active {
  background: var(--select-selected-bg);
  color: var(--select-selected-fg);
  font-weight: 600;
  border-color: transparent;
}
.fam-region-pop__empty {
  grid-column: 1 / -1;
  padding: 18px;
  text-align: center;
  font-size: 12px;
  color: var(--text-dim);
}
/* tabs 内诚实空态（未绑 Cookie / 未同步家庭组） */
.lib-empty-tab {
  text-align: center;
  padding: 26px 16px;
  border: 1px dashed var(--border-soft);
  border-radius: var(--radius);
  background: var(--surface-inset);
  font-size: 12.5px;
  color: var(--text-secondary);
}
</style>
