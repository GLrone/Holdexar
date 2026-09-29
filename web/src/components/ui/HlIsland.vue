<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { crawlApi } from '@/api/client'
import { usePriceStatus } from '@/composables/usePriceStatus'
import { useI18n } from '@/locales'
import { useCrawlStatusStore } from '@/stores/crawlStatus'

import HlIcon from './HlIcon.vue'
import HlSpinner from './HlSpinner.vue'
import {
  dropNotice,
  island,
  muteTone,
  setExpanded,
  setHovered,
  type IslandTone,
} from './island'

/**
 * 灵动岛消息面：顶部悬浮的独立胶囊，四态就地撑开与收拢。
 *   idle     —— 价格更新结论，仅在数据已旧时占岛；无消息无任务且数据新鲜时
 *               整枚收回，不占常驻位
 *   notice   —— 单行消息，宽度随文案伸缩
 *   task     —— 进行中项（消息 / 全局注册任务 / 爬取任务），带定量/滑动进度条
 *   expanded —— 详情与动作；无消息无任务时展开的是价格状态面
 *               （重试 / 跳转 / 忽略 / 静音 / 任务控制 / 价格状态说明）
 *
 * 头部行（图标 + 文案 + 尾部槽位）是**跨态同一组元素**：撑开时只换布局，
 * 不换实例、不重算配色，避免展开瞬间文字与图标闪一下。展开面按
 * leading（图标）/ trailing（关键数值）/ bottom（正文与动作）分区，
 * 正文与动作沿过冲曲线分级落入。
 *
 * 跳转只由展开面里的动作按钮发起：点岛体自身恒为开合，不承担导航。
 *
 * 岛体是单层元素：底色、内描边、外投影全部落在根元素上，内容层以 inherit
 * 圆角裁切。视图层唯一实例，挂在 App 外壳。
 */

const router = useRouter()
const crawl = useCrawlStatusStore()
const { t } = useI18n()

/* 空闲态即价格更新状态：结论与展开面正文同一数据源、同一口径 */
const {
  kind: priceKind,
  label: priceLabel,
  color: priceColor,
  tip: priceTip,
  detail: priceDetail,
  stale: priceStale,
} = usePriceStatus()

const TONE_ICON: Record<IslandTone, string> = {
  success: 'check-circle',
  error: 'x-circle',
  warning: 'warning',
  info: 'info',
  progress: 'refresh',
}

/* 四态尺寸：idle / notice 的宽度按文案长度算出，其余定值。idle 与 notice 取
   半高圆角（胶囊），task 内含按钮与进度条、全圆角会吃掉两端可用宽度，故收窄
   到 20；expanded 再放开到 28 显「撑开」。展开高度由内容实数算出，见
   expandedHeight。 */
const SHAPE = {
  idle: { h: 30, r: 15 },
  notice: { h: 34, r: 17 },
  task: { w: 348, h: 58, r: 20 },
  expanded: { w: 420, r: 28 },
} as const

/* ── 展开态的高度分档 ──
   岛宽 420，扣除内边距、图标底托与尾部槽位后**可用字宽 350**。文案在展开态
   换行，行数取上界；正文按两行预留，动作行恒在。各常数按对应分档的行数配置，
   比内容略高一点，不会把文案压掉。 */
const EXPANDED_TEXT_W = 350
/** 头部文案在展开态最多显示的行数，超出由 line-clamp 收尾并另给全文提示 */
const EXPANDED_MAX_LINES = 4
/** 13px 字号 × 1.3 行高 */
const EXPANDED_LINE_H = 17
/** 图标底托高度：一行文案时头部行由它撑起 */
const EXPANDED_LEAD_H = 26
/** 上下内边距 + 头部与正文的间隔 + 动作行 */
const EXPANDED_CHROME_H = 63
/** 正文两行（12px × 1.6） */
const EXPANDED_DETAIL_H = 39
/** 进度条 + 它与头部的间隔 */
const EXPANDED_BAR_H = 12

/** 宽字形（CJK / 假名 / 谚文 / 全角标点）在 13px 字号下按整字宽计，其余按半角。
    CJK 字面宽度就等于字号，这里是精确值；半角按 10px 计明显高于实际排版宽度
    （拉丁平均约 7.5px），换来的是**估算行数恒为实际上界**——宁可岛高一点也不裁
    文案。10 是半角单字宽的下限：取值 8~9.5 会让个别文案少算一行。 */
const WIDE_GLYPH = /[\u1100-\u115F\u2E80-\uA4CF\uAC00-\uD7FF\uF900-\uFAFF\uFE30-\uFE6F\uFF00-\uFFEF]/

/** 尾部槽位（「还有 N 条」）在展开态占的宽度，含它与文案的间距 */
const EXPANDED_TRAIL_W = 72

function headLineCount(text: string, wrapWidth: number): number {
  let width = 0
  for (const ch of text) width += WIDE_GLYPH.test(ch) ? 13 : 10
  return Math.max(1, Math.ceil(width / wrapWidth))
}

/** 排队气泡最多露出的层数：再多也只用两层表达「还有」 */
const DECK_MAX = 2

const rootEl = ref<HTMLElement | null>(null)

const current = computed(() => island.notices[0] ?? null)
const waiting = computed(() => Math.max(0, island.notices.length - 1))
const taskActive = computed(() => crawl.running && crawl.total > 0)
/** 全局长任务（store 注册）：消息队列为空时的常驻进度位，跨页存活 */
const activeTask = computed(() => island.tasks[0] ?? null)
const moreTasks = computed(() => Math.max(0, island.tasks.length - 1))

const mode = computed<'idle' | 'notice' | 'task' | 'expanded'>(() => {
  if (island.expanded) return 'expanded'
  if (current.value) return current.value.sticky ? 'task' : 'notice'
  if (activeTask.value || taskActive.value) return 'task'
  return 'idle'
})

/** 没有消息也没有任务：岛上承载的是价格更新结论（空闲态与它的展开面共用） */
const showingPrice = computed(() => !current.value && !taskActive.value && !activeTask.value)

/** 收回判据：有消息、有任务或数据已旧才显示岛体；无消息无任务且数据新鲜时整枚离场 */
const visible = computed(() => mode.value !== 'idle' || priceStale.value)

/** 任务态文案：进行中消息优先，其次注册任务，最后爬取任务 */
const taskText = computed(() => {
  const n = current.value
  if (n && n.sticky) return n.text
  if (activeTask.value) return activeTask.value.text
  return t('island.task.running', { done: progDone.value, total: progTotal.value })
})

/* 进度口径：轮次累计优先——一轮价格刷新由多个任务段串成，按轮累计使进度与
   百分比在轮内单调推进；非轮次任务（手动抓取 / 修复）用当前任务段计数 */
const progDone = computed(() => crawl.roundProgress?.done ?? crawl.done)
const progTotal = computed(() => crawl.roundProgress?.total ?? crawl.total)

/** 定量进度百分比；给不到总量时返回 null（改走滑动条） */
const taskPercent = computed<number | null>(() => {
  const n = current.value
  if (n && n.sticky) return null
  if (activeTask.value) return activeTask.value.percent
  if (!progTotal.value) return null
  return Math.min(100, Math.round((progDone.value / progTotal.value) * 100))
})

/** 头部行「取消任务」键只对爬取任务成立：注册任务与消息气泡没有对应端点 */
const crawlOnlyTask = computed(() => !current.value && !activeTask.value && taskActive.value)

const expandedIsTask = computed(() => !current.value && taskActive.value)
/** 展开面呈现注册任务清单：消息未占用展示位且有任务在跑 */
const expandedShowsTasks = computed(() => !current.value && !!activeTask.value)
const canRetry = computed(() => !!current.value?.retry)

/** 进行中：占住展示槽位的 sticky 消息、注册任务或正在跑的爬取任务，都带进度条 */
const busy = computed(() => !!current.value?.sticky || taskActive.value || !!activeTask.value)

/** 展开态任务清单行：头部行已展示最新一个任务，这里只列其余的在跑任务 */
const taskLines = computed(() => island.tasks.slice(1, 4))

/** 注册任务的「查看」落点：展开面动作跳发起方页面 */
const taskGoto = computed(() => activeTask.value?.to ?? '')

/** 详情动作落点：调用方指定优先，错误消息缺省落日志页 */
const goto = computed(() => {
  const n = current.value
  if (!n) return ''
  return n.to || (n.tone === 'error' ? '/logs' : '')
})

const gotoLabel = computed(() => {
  const n = current.value
  if (!n) return ''
  if (n.toLabel) return n.toLabel
  return goto.value === '/logs' ? t('island.viewLogs') : t('island.view')
})

/** 头部文案：四种态共用一个元素，价格面撑开前后文本不变，因而无重排闪烁 */
const headText = computed(() => {
  if (showingPrice.value) return priceLabel.value
  const n = current.value
  if (n) return n.text
  return taskText.value
})

/** 头部图标：价格面用刷新型，其余态按消息语义取型 */
const leadIcon = computed(() => {
  if (showingPrice.value) return 'refresh'
  const n = current.value
  return n ? TONE_ICON[n.tone] : 'refresh'
})

/** 转圈替代图标：价格恰在更新、进行中的消息、无头任务 */
const leadSpin = computed(() => {
  if (showingPrice.value) return priceKind.value === 'running'
  if (mode.value === 'task') return true
  return !!current.value?.sticky
})

/** 图标配色：价格面由内联样式接管（随结论变色），其余按消息 tone 取类 */
const leadToneClass = computed(() => {
  if (showingPrice.value) return ''
  const n = current.value
  if (!n) return 'is-progress'
  return `is-${n.tone}`
})

const showBar = computed(() => busy.value)

/** 尾部槽位：展开态放关键数值——在跑任务的完成百分比优先，多余任务数与
   排队条数退居其次（排队形态已由岛体下方的堆叠气泡表达） */
const trailing = computed(() => {
  if (mode.value !== 'expanded') return ''
  if (activeTask.value && taskPercent.value !== null) return `${taskPercent.value}%`
  if (moreTasks.value) return t('island.more', { n: moreTasks.value })
  if (taskActive.value && taskPercent.value !== null) return `${taskPercent.value}%`
  if (waiting.value) return t('island.more', { n: waiting.value })
  return ''
})

/** 注册任务收场前的落点跳转（任务本体由发起方 store 继续推进） */
function goTask() {
  const target = taskGoto.value
  if (!target) return
  setExpanded(false)
  void router.push(target)
}

/** 展开面正文：有消息时是消息补充说明，价格面是数据有多旧 */
const detailText = computed(() => {
  if (current.value) return current.value.detail ?? ''
  return showingPrice.value ? priceDetail.value : ''
})

/** 排队堆叠层数：岛体下方浮出的缩进气泡，把「还有几条」做成看得见的形态 */
const deckDepth = computed(() => Math.min(DECK_MAX, waiting.value))

/** 状态文案宽度：中英文长度差大，按字符数伸缩并夹在区间内 */
const idleWidth = computed(() => Math.min(240, Math.max(124, 60 + priceLabel.value.length * 13)))

const noticeWidth = computed(() => {
  const n = current.value
  if (!n) return 168
  return Math.min(400, Math.max(168, 76 + n.text.length * 13))
})

/** 展开态可用字宽：尾部槽位出现时先给它让位 */
const headWrapWidth = computed(() => EXPANDED_TEXT_W - (trailing.value ? EXPANDED_TRAIL_W : 0))

/** 展开态头部行数上界（未封顶）：换行后岛体要跟着变高，故先算出占几行 */
const headRawLines = computed(() => headLineCount(headText.value, headWrapWidth.value))

/** 实际显示的行数：岛体高度只按它算 */
const headLines = computed(() => Math.min(EXPANDED_MAX_LINES, headRawLines.value))

/** 行数上界已到顶 = 文案被 line-clamp 收尾，此时用全文 tooltip 兜住读不到的部分 */
const headTruncated = computed(() => headRawLines.value > EXPANDED_MAX_LINES)

/** 展开态的多行文案：仅此情况才把头部行改成顶对齐，单行与别的态一律居中 */
const headStacked = computed(() => mode.value === 'expanded' && headLines.value > 1)

/** 头部文案的行内样式：价格面染色 + 展开态的行数上限（与高度分档同一常数） */
const headTextStyle = computed(() => {
  const style: Record<string, string> = {}
  if (showingPrice.value) style.color = priceColor.value
  if (mode.value === 'expanded') style.webkitLineClamp = String(EXPANDED_MAX_LINES)
  return style
})

/** 展开态高度：头部行高（一行时由图标底托撑起）+ 正文 + 进度条，逐段相加 */
const expandedHeight = computed(() => {
  const row = Math.max(EXPANDED_LEAD_H, headLines.value * EXPANDED_LINE_H)
  const detail = detailText.value ? EXPANDED_DETAIL_H : 0
  const bar = showBar.value ? EXPANDED_BAR_H : 0
  return EXPANDED_CHROME_H + row + detail + bar
})

const size = computed(() => {
  const m = mode.value
  if (m === 'idle') return { w: idleWidth.value, h: SHAPE.idle.h, r: SHAPE.idle.r }
  if (m === 'notice') return { w: noticeWidth.value, h: SHAPE.notice.h, r: SHAPE.notice.r }
  if (m === 'expanded') return { w: SHAPE.expanded.w, h: expandedHeight.value, r: SHAPE.expanded.r }
  return { w: SHAPE.task.w, h: SHAPE.task.h, r: SHAPE.task.r }
})

const islandStyle = computed(() => ({
  width: `${size.value.w}px`,
  height: `${size.value.h}px`,
  borderRadius: `${size.value.r}px`,
}))

/** 点岛体恒为开合详情面：跳转只由展开面里的动作按钮发起 */
function onIslandClick() {
  setExpanded(!island.expanded)
}

function dismiss() {
  const n = current.value
  setExpanded(false)
  if (n) dropNotice(n.id)
}

function mute() {
  const n = current.value
  setExpanded(false)
  if (n) muteTone(n.tone)
}

function retry() {
  const n = current.value
  const fn = n?.retry ?? null
  setExpanded(false)
  if (n) dropNotice(n.id)
  fn?.()
}

function goDetail() {
  const target = goto.value
  if (!target) return
  setExpanded(false)
  void router.push(target)
}

/** 停止运行中的爬取任务；任务状态由 SSE 推送，此处不另起提示 */
async function cancelTask() {
  const id = crawl.activeJobId
  setExpanded(false)
  if (id === null) return
  try {
    await crawlApi.stop(id)
  } catch {
    /* 请求失败不改岛的展示：running 位由后续 job.status 事件收束 */
  }
}

function openTask() {
  setExpanded(false)
  void router.push('/crawl')
}

function onDocDown(e: MouseEvent) {
  if (!island.expanded) return
  if (rootEl.value?.contains(e.target as Node)) return
  setExpanded(false)
}

onMounted(() => document.addEventListener('mousedown', onDocDown))
onBeforeUnmount(() => document.removeEventListener('mousedown', onDocDown))
</script>

<template>
  <Transition name="island-retract">
    <div
      v-if="visible"
      ref="rootEl"
      class="hl-island"
      :class="[`hl-island--${mode}`, { 'is-stacked': headStacked }]"
      :style="islandStyle"
      role="status"
      aria-live="polite"
      :aria-label="t('island.label')"
      :title="mode === 'idle' ? priceTip || undefined : undefined"
      @mouseenter="setHovered(true)"
      @mouseleave="setHovered(false)"
      @click="onIslandClick"
    >
    <div class="hl-island__body">
      <!-- 头部行：四态共用一组元素，展开只换布局，不换实例 -->
      <div class="hl-island__row">
        <span class="hl-island__lead">
          <HlSpinner v-if="leadSpin" class="hl-island__spin" />
          <HlIcon
            v-else
            :name="leadIcon"
            :size="13"
            class="hl-island__icon"
            :class="leadToneClass"
            :style="showingPrice ? { color: priceColor } : undefined"
          />
        </span>
        <span
          class="hl-island__text"
          :class="{ 'is-status': showingPrice }"
          :style="headTextStyle"
          :title="headTruncated ? headText : undefined"
        >
          {{ headText }}
        </span>
        <span v-if="trailing" class="hl-island__trail">{{ trailing }}</span>
        <button v-if="crawlOnlyTask && mode === 'task'" class="hl-island__btn is-tiny" @click.stop="cancelTask">
          {{ t('island.task.cancel') }}
        </button>
      </div>

      <!-- 进度条：任务态常显，展开中的任务同样保留（关键数值不因撑开而消失） -->
      <span v-if="showBar" class="hl-island__bar" :class="{ 'is-slide': taskPercent === null }">
        <i :style="taskPercent === null ? undefined : { width: `${taskPercent}%` }" />
      </span>

      <!-- 展开面：正文与动作分级落入；收起时整块先淡出，再由岛体收拢 -->
      <Transition name="island-fold">
        <div v-if="mode === 'expanded'" class="hl-island__fold">
          <!-- 注册任务清单面：头部行之外还有在跑任务时逐行列出，动作出路是发起方页面 -->
          <template v-if="expandedShowsTasks">
            <div v-if="taskLines.length" class="hl-island__tasks">
              <span v-for="task in taskLines" :key="task.key" class="hl-island__taskline">
                <span class="hl-island__tasktext">{{ task.text }}</span>
                <b v-if="task.percent !== null" class="hl-island__taskpct">{{ task.percent }}%</b>
              </span>
            </div>
            <span v-else class="hl-island__spacer" />
            <div v-if="taskGoto" class="hl-island__actions">
              <button class="hl-island__btn is-primary" @click.stop="goTask">
                {{ t('island.view') }}
              </button>
            </div>
            <span v-else class="hl-island__spacer" />
          </template>
          <template v-else>
            <p v-if="detailText" class="hl-island__detail">{{ detailText }}</p>
            <span v-else class="hl-island__spacer" />

            <div class="hl-island__actions">
              <template v-if="expandedIsTask">
                <button class="hl-island__btn is-primary" @click.stop="openTask">
                  {{ t('island.task.open') }}
                </button>
                <button class="hl-island__btn" @click.stop="cancelTask">
                  {{ t('island.task.cancel') }}
                </button>
              </template>
              <!-- 价格状态面：唯一的动作出路是任务中心，没有可忽略或静音的消息 -->
              <template v-else-if="showingPrice">
                <button class="hl-island__btn is-primary" @click.stop="openTask">
                  {{ t('island.task.open') }}
                </button>
              </template>
              <template v-else>
                <button v-if="canRetry" class="hl-island__btn is-primary" @click.stop="retry">
                  {{ t('island.retry') }}
                </button>
                <button
                  v-if="goto"
                  class="hl-island__btn"
                  :class="{ 'is-primary': !canRetry }"
                  @click.stop="goDetail"
                >
                  {{ gotoLabel }}
                </button>
                <button class="hl-island__btn" @click.stop="dismiss">{{ t('island.dismiss') }}</button>
                <button class="hl-island__link" @click.stop="mute">{{ t('island.mute') }}</button>
              </template>
            </div>
          </template>
        </div>
      </Transition>
    </div>

    <!-- 排队堆叠：多出一条消息时岛体下方浮出缩进气泡，逐层收窄压暗 -->
    <Transition name="island-deck">
      <span v-if="deckDepth" class="hl-island__deck" aria-hidden="true">
        <i v-for="n in deckDepth" :key="n" />
      </span>
    </Transition>
    </div>
  </Transition>
</template>

<style scoped>
/* 独立悬浮的胶囊：不贴视口上沿，四角全部圆角，形状只由宽高与圆角插值。
   水平锚点是**视口**中心：固定定位下百分比按视口算，不随侧栏宽度或折叠漂移 */
.hl-island {
  position: fixed;
  top: var(--island-top);
  left: 50%;
  transform: translateX(-50%);
  z-index: 9999;
  color: var(--text-primary);
  font-family: var(--font-sans);
  background: var(--island-bg);
  cursor: pointer;
  /* 内描边 + 双层外投影，整体随主题翻转（见 tokens.css 的 --shadow-island） */
  box-shadow: var(--shadow-island);
  transition:
    width calc(var(--duration-5) * var(--motion-scale)) var(--ease-island),
    height calc(var(--duration-5) * var(--motion-scale)) var(--ease-island),
    border-radius calc(var(--duration-5) * var(--motion-scale)) var(--ease-island);
}

/* 内容层：与岛体同圆角裁切，形变过程中内容不会溢出岛体 */
.hl-island__body {
  position: absolute;
  inset: 0;
  border-radius: inherit;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  justify-content: center;
  padding: 0 16px;
  box-sizing: border-box;
}

.hl-island--task .hl-island__body {
  gap: 9px;
}

.hl-island--expanded .hl-island__body {
  justify-content: flex-start;
  padding: 14px 18px 13px;
}

.hl-island__row {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  min-width: 0;
  flex: 0 0 auto;
}

/* 空闲态是一句状态结论，居中而非左对齐 */
.hl-island--idle .hl-island__row {
  justify-content: center;
}

/* leading：展开态升格为一块圆角底托，给图标一个可落脚的方块 */
.hl-island__lead {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
  transition:
    width calc(var(--duration-4) * var(--motion-scale)) var(--ease-island),
    height calc(var(--duration-4) * var(--motion-scale)) var(--ease-island),
    background-color calc(var(--duration-4) * var(--motion-scale)) var(--ease-out);
}

.hl-island--expanded .hl-island__lead {
  width: 26px;
  height: 26px;
  border-radius: 8px;
  background: var(--island-btn);
}

.hl-island__icon {
  color: var(--text-muted);
  flex: 0 0 auto;
}

.hl-island__icon.is-success {
  color: var(--success);
}

.hl-island__icon.is-error {
  color: var(--danger);
}

.hl-island__icon.is-warning {
  color: var(--warning);
}

.hl-island__icon.is-info {
  color: var(--info);
}

.hl-island__icon.is-progress {
  color: var(--accent);
}

.hl-island__icon.is-spin {
  animation: hlIslandSpin 1.1s linear infinite;
}

@keyframes hlIslandSpin {
  to {
    transform: rotate(360deg);
  }
}

.hl-island__spin {
  width: 12px;
  height: 12px;
  flex: 0 0 auto;
}

.hl-island__text {
  font-size: 12px;
  line-height: 1.3;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1 1 auto;
  min-width: 0;
  transition:
    font-size calc(var(--duration-4) * var(--motion-scale)) var(--ease-out),
    color calc(var(--duration-3) * var(--motion-scale)) var(--ease-out);
}

/* 状态文案宽度即胶囊宽度，不参与剩余空间分配 */
.hl-island__text.is-status {
  flex: 0 1 auto;
  font-weight: 500;
}

/* 展开态头部升格为正文：单行省略换成换行（行数上限由组件按 EXPANDED_MAX_LINES
   行内下发，与高度分档同一常数） */
.hl-island--expanded .hl-island__text {
  display: -webkit-box;
  -webkit-box-orient: vertical;
  font-size: 13px;
  font-weight: 500;
  white-space: normal;
  overflow-wrap: anywhere;
  text-overflow: clip;
}

/* 多行文案时头部行按顶对齐：图标底托与尾部数值贴第一行，不在整块里居中 */
.hl-island--expanded.is-stacked .hl-island__row {
  align-items: flex-start;
}

/* trailing：展开态的关键数值（排队条数 / 完成百分比） */
.hl-island__trail {
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  color: var(--text-muted);
  flex: 0 0 auto;
  animation: hlIslandDrop 280ms var(--ease-island) 60ms both;
}

/* 展开面：撑开时高度变化交给岛体，内容按自己的节奏落位 */
.hl-island__fold {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  gap: 8px;
}

.island-fold-leave-active {
  transition:
    opacity calc(var(--duration-2) * var(--motion-scale)) var(--ease-out),
    transform calc(var(--duration-2) * var(--motion-scale)) var(--ease-out);
}

.island-fold-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}

.hl-island__spacer {
  flex: 1 1 auto;
}

.hl-island__detail {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-muted);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  animation: hlIslandDrop 320ms var(--ease-island) 40ms both;
}

/* 注册任务清单面：一行一任务，定量任务行尾带百分比 */
.hl-island__tasks {
  flex: 1 1 auto;
  min-height: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 7px;
}

.hl-island__taskline {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
  animation: hlIslandDrop 300ms var(--ease-island) both;
}

.hl-island__tasktext {
  font-size: 12px;
  line-height: 1.5;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1 1 auto;
  min-width: 0;
}

.hl-island__taskpct {
  font-size: 11px;
  font-weight: 500;
  font-variant-numeric: tabular-nums;
  color: var(--text-muted);
  flex: 0 0 auto;
}

.hl-island__bar {
  position: relative;
  display: block;
  height: 3px;
  border-radius: 2px;
  overflow: hidden;
  background: var(--surface-track);
  flex: 0 0 auto;
}

.hl-island__bar > i {
  position: absolute;
  top: 0;
  bottom: 0;
  left: 0;
  width: 40%;
  border-radius: 2px;
  background: var(--accent);
}

.hl-island__bar.is-slide > i {
  animation: hlIslandBar 1.4s var(--ease-inout) infinite;
}

@keyframes hlIslandBar {
  from {
    transform: translateX(-100%);
  }
  to {
    transform: translateX(350%);
  }
}

.hl-island__actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 0 0 auto;
}

/* 动作逐个落位（收起时不参与，动作整块随 fold 一起淡出） */
.hl-island__actions > * {
  animation: hlIslandDrop 300ms var(--ease-island) both;
}

.hl-island__actions > *:nth-child(1) {
  animation-delay: 100ms;
}

.hl-island__actions > *:nth-child(2) {
  animation-delay: 140ms;
}

.hl-island__actions > *:nth-child(3) {
  animation-delay: 180ms;
}

.hl-island__actions > *:nth-child(4) {
  animation-delay: 220ms;
}

@keyframes hlIslandDrop {
  from {
    opacity: 0;
    transform: translateY(-6px);
  }
  to {
    opacity: 1;
    transform: none;
  }
}

.hl-island__btn {
  border: none;
  border-radius: 8px;
  padding: 5px 12px;
  font-size: 11.5px;
  font-family: inherit;
  color: var(--text-primary);
  background: var(--island-btn);
  cursor: pointer;
  transition: opacity var(--transition);
}

.hl-island__btn.is-primary {
  background: var(--accent-fill);
  color: var(--on-accent-fill);
}

.hl-island__btn.is-tiny {
  padding: 3px 9px;
  font-size: 11px;
  flex: 0 0 auto;
}

.hl-island__btn:hover {
  opacity: 0.82;
}

.hl-island__link {
  border: none;
  background: transparent;
  padding: 4px 2px;
  margin-left: auto;
  font-size: 11.5px;
  font-family: inherit;
  color: var(--text-muted);
  cursor: pointer;
}

.hl-island__link:hover {
  color: var(--text-primary);
}

/* 排队堆叠：岛体下方浮出的缩进气泡，越远的一层越窄越淡 */
.hl-island__deck {
  position: absolute;
  left: 0;
  right: 0;
  top: 100%;
  margin-top: -5px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  pointer-events: none;
}

.hl-island__deck > i {
  display: block;
  height: 8px;
  border-radius: 4px;
  background: var(--island-bg);
  box-shadow: var(--shadow-island);
}

.hl-island__deck > i:nth-child(1) {
  width: 86%;
}

.hl-island__deck > i:nth-child(2) {
  width: 72%;
  opacity: 0.72;
}

.island-deck-enter-active,
.island-deck-leave-active {
  transition: opacity calc(var(--duration-3) * var(--motion-scale)) var(--ease-out);
}

.island-deck-enter-from,
.island-deck-leave-to {
  opacity: 0;
}

/* 收回与浮出：无消息且数据新鲜时整枚离场；位移 + 透明度，居中位移全程保留 */
.island-retract-enter-active,
.island-retract-leave-active {
  transition:
    opacity calc(var(--duration-4) * var(--motion-scale)) var(--ease-out),
    transform calc(var(--duration-4) * var(--motion-scale)) var(--ease-island);
}

.island-retract-enter-from,
.island-retract-leave-to {
  opacity: 0;
  transform: translateX(-50%) translateY(-12px);
}

/* 循环与一次性入场动画按动效红线显式关停：--motion-scale 只作用于 transition */
@media (prefers-reduced-motion: reduce) {
  .hl-island__bar.is-slide > i,
  .hl-island__icon.is-spin {
    animation: none;
  }

  .hl-island__trail,
  .hl-island__detail,
  .hl-island__taskline,
  .hl-island__actions > * {
    animation: none;
  }
}
</style>
