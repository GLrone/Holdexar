<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { Delete, Edit, Plus, Search } from '@element-plus/icons-vue'

import {
  alertsApi,
  ratesApi,
  type AlertEventItem,
  type PriceAlertItem,
  type GameSearchResult,
} from '@/api/client'
import { formatCnyFen, flagUrl } from '@/api/regions'
import { currencyName, formatMinor } from '@/api/currencies'
import { useI18n } from '@/locales'
import { useRegionsStore } from '@/stores/regions'
import RegionFlag from '@/components/RegionFlag.vue'
import HlSelect, { type HlSelectOption } from '@/components/ui/HlSelect.vue'
import HlImg from '@/components/ui/HlImg.vue'
import HlTooltip from '@/components/ui/HlTooltip.vue'
import { HlEmpty, message } from '@/components/ui'

const { t } = useI18n()
const router = useRouter()
const regionsStore = useRegionsStore()

const alerts = ref<PriceAlertItem[]>([])
const events = ref<AlertEventItem[]>([])
const loading = ref(true)

// ─── 添加规则：游戏搜索 ───
const searchQuery = ref('')
const searchResults = ref<GameSearchResult[]>([])
const searchError = ref('')
const searching = ref(false)
const selectedGame = ref<GameSearchResult | null>(null)

const form = reactive({
  appid: 0,
  region: 'CN',
  targetType: 'price',
  targetValue: '',
})

/** 地区下拉选项（带国旗） */
const regionSelectOptions = computed<HlSelectOption[]>(() =>
  regionsStore.metas.map((r) => ({
    value: r.code,
    label: r.name,
    flag: flagUrl(r.code),
  })),
)

/** 目标类型下拉选项。**computed 而非模块级常量**：模块级常量只在模块加载时求值一次，
 *  里面的 t() 会把语言冻死在那一刻；computed 在渲染期求值，切语言即重算。 */
const targetTypeOptions = computed<HlSelectOption[]>(() => [
  { value: 'price', label: t('alerts.rules.optionPrice') },
  { value: 'pct', label: t('alerts.rules.optionPct') },
  { value: 'historic_low', label: t('alerts.rules.optionHistoricLow') },
])

const targetText = computed(() => {
  if (form.targetType === 'historic_low') return t('alerts.rules.hintHistoric')
  if (form.targetType === 'price') return t('alerts.rules.hintPrice')
  return t('alerts.rules.hintPct')
})

// ─── 元 ↔ 该区货币换算（阈值统一按人民币元设置，外区规则悬停换算）───
// 汇率取 GET /rates 现值（rateToCny = 1 单位外币兑人民币元）；拉取失败只
// 是换算气泡不展示，规则增删改不受影响。
const rateMap = ref<Map<string, number>>(new Map())

const regionCurrencyMap = computed(() => {
  const m = new Map<string, string>()
  regionsStore.metas.forEach((r) => m.set(r.code.toUpperCase(), (r.currency || 'CNY').toUpperCase()))
  return m
})

function regionCurrency(region: string): string {
  return regionCurrencyMap.value.get(region.toUpperCase()) ?? 'CNY'
}

function isForeignRegion(region: string): boolean {
  return regionCurrency(region) !== 'CNY'
}

/** 人民币分 → 该区货币最小单位（无汇率返回 null） */
function cnyFenToLocal(cnyFen: number, region: string): number | null {
  const rate = rateMap.value.get(regionCurrency(region))
  return rate ? Math.round(cnyFen / rate) : null
}

/** 元输入的实时换算气泡：仅 price 类 + 外区 + 合法正数时非空 */
function convertHint(region: string, type: string, valueStr: string): string {
  if (type !== 'price' || !isForeignRegion(region)) return ''
  const v = Number(valueStr)
  if (!valueStr.trim() || !Number.isFinite(v) || v <= 0) return ''
  const local = cnyFenToLocal(Math.round(v * 100), region)
  if (local === null) return ''
  const currency = regionCurrency(region)
  return t('alerts.rules.convertHint', { value: formatMinor(local, currency), name: currencyName(currency) })
}

const addConvertHint = computed(() => convertHint(form.region, form.targetType, form.targetValue))
const editConvertHint = computed(() =>
  editingId.value === null ? '' : convertHint(editForm.region, editForm.targetType, editForm.targetValue),
)

/** 规则条件列的悬停换算文本（price 类外区规则：¥ 目标位 → 本币约价） */
function ruleHoverText(row: PriceAlertItem): string {
  if (row.targetType !== 'price' || row.targetValue === null || !isForeignRegion(row.region)) return ''
  const local = cnyFenToLocal(row.targetValue, row.region)
  if (local === null) return ''
  const currency = regionCurrency(row.region)
  return t('alerts.rules.convertHint', { value: formatMinor(local, currency), name: currencyName(currency) })
}

/** 历史行「当时价格」主文本：人民币快照优先，无快照的旧事件回退本币文本 */
function eventPriceText(row: AlertEventItem): string {
  if (row.priceCny !== null) return formatCnyFen(row.priceCny)
  return row.priceText || '—'
}

/** 历史行「当时价格」悬停文本：外区且有人民币快照时展示本币原价 */
function eventHoverText(row: AlertEventItem): string {
  return row.priceCny !== null && isForeignRegion(row.region) ? row.priceText : ''
}

async function searchGames() {
  const q = searchQuery.value.trim()
  if (!q) return
  searching.value = true
  searchError.value = ''
  try {
    const res = await alertsApi.search(q, form.region)
    searchResults.value = res.items
    searchError.value = res.error ?? ''
    if (res.items.length === 0 && searchError.value) {
      message.warning(searchError.value)
    }
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    searching.value = false
  }
}

function selectGame(game: GameSearchResult) {
  selectedGame.value = game
  form.appid = game.appid
  searchResults.value = []
  searchQuery.value = game.name || String(game.appid)
}

function clearSearch() {
  selectedGame.value = null
  searchResults.value = []
  searchError.value = ''
  searchQuery.value = ''
  form.appid = 0
}

async function load() {
  loading.value = true
  try {
    alerts.value = await alertsApi.list()
    events.value = await alertsApi.events(50)
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    loading.value = false
  }
}

async function add() {
  if (!form.appid) {
    message.warning(t('alerts.toast.selectGameFirst'))
    return
  }
  let value = form.targetValue.trim() === '' ? undefined : Number(form.targetValue)
  if (form.targetType !== 'historic_low' && value === undefined) {
    message.warning(t('alerts.toast.enterTarget'))
    return
  }
  // price 类按元输入 → 人民币分（阈值口径）；pct 保持百分数
  if (form.targetType === 'price' && value !== undefined) value = Math.round(value * 100)
  try {
    await alertsApi.add(form.appid, form.region, form.targetType, value)
    message.success(t('alerts.toast.added'))
    clearSearch()
    form.targetValue = ''
    await load()
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  }
}

async function toggle(alert: PriceAlertItem) {
  await alertsApi.update(alert.id, { active: !alert.active })
  message.success(alert.active ? t('alerts.toast.disabled') : t('alerts.toast.enabled'))
  await load()
}

async function remove(alert: PriceAlertItem) {
  /* 游戏名兜底（AppID 串）与整句一起走 {name} 参数：不做「删除 #」+「规则（」+「）？」
     的片段拼接——中英括号形态与语序都不同，拼不出来。 */
  const name = alert.gameName || `AppID ${alert.appid}`
  const ok = await ElMessageBox.confirm(
    t('alerts.rules.removeConfirm', { id: alert.id, name }),
    t('alerts.rules.confirmTitle'),
    { type: 'warning' },
  ).then(() => true).catch(() => false)
  if (!ok) return
  try {
    await alertsApi.remove(alert.id)
    message.success(t('alerts.toast.removed', { id: alert.id }))
    await load()
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  }
}

// ─── 触发历史删除（单条 / 全部）───
async function removeEvent(ev: AlertEventItem) {
  const name = ev.gameName || `AppID ${ev.appid}`
  const ok = await ElMessageBox.confirm(
    t('alerts.history.removeConfirm', { name }),
    t('alerts.rules.confirmTitle'),
    { type: 'warning' },
  ).then(() => true).catch(() => false)
  if (!ok) return
  try {
    await alertsApi.removeEvent(ev.id)
    message.success(t('alerts.toast.eventRemoved'))
    await load()
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  }
}

async function clearAllEvents() {
  const ok = await ElMessageBox.confirm(
    t('alerts.history.clearConfirm', { count: events.value.length }),
    t('alerts.rules.confirmTitle'),
    { type: 'warning' },
  ).then(() => true).catch(() => false)
  if (!ok) return
  try {
    const res = await alertsApi.clearEvents()
    message.success(t('alerts.toast.historyCleared', { count: res.removed }))
    await load()
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  }
}

// ─── 编辑规则 ───
const editingId = ref<number | null>(null)
const editForm = reactive({
  targetType: 'price',
  targetValue: '',
  region: 'CN',
})

function startEdit(alert: PriceAlertItem) {
  editingId.value = alert.id
  editForm.targetType = alert.targetType
  // price 类阈值以人民币分存储，编辑时回显为元；pct 原样回显
  editForm.targetValue =
    alert.targetValue !== null
      ? String(alert.targetType === 'price' ? alert.targetValue / 100 : alert.targetValue)
      : ''
  editForm.region = alert.region
}

function cancelEdit() {
  editingId.value = null
}

async function saveEdit(alert: PriceAlertItem) {
  let value = editForm.targetValue.trim() === '' ? undefined : Number(editForm.targetValue)
  if (editForm.targetType !== 'historic_low' && value === undefined) {
    message.warning(t('alerts.toast.enterTarget'))
    return
  }
  // price 类按元输入 → 人民币分（阈值口径）；pct 保持百分数
  if (editForm.targetType === 'price' && value !== undefined) value = Math.round(value * 100)
  try {
    await alertsApi.update(alert.id, {
      targetType: editForm.targetType,
      targetValue: value,
      region: editForm.region,
    })
    message.success(t('alerts.toast.updated'))
    editingId.value = null
    await load()
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  }
}

// ─── SMTP 邮件设置 ───
const smtpLoading = ref(false)
const smtpSaving = ref(false)
const smtpForm = reactive({
  host: '',
  port: 465,
  user: '',
  password: '',
  toAddr: '',
  useSsl: true,
})
const smtpHasPassword = ref(false)
const smtpMasked = ref('')

async function loadSmtp() {
  smtpLoading.value = true
  try {
    const cfg = await alertsApi.getSmtp()
    smtpForm.host = cfg.host
    smtpForm.port = cfg.port
    smtpForm.user = cfg.user
    smtpForm.toAddr = cfg.toAddr
    smtpForm.useSsl = cfg.useSsl
    smtpHasPassword.value = cfg.hasPassword
    smtpMasked.value = cfg.password
    smtpForm.password = ''
  } catch {
    // 静默
  } finally {
    smtpLoading.value = false
  }
}

async function saveSmtp() {
  smtpSaving.value = true
  try {
    const payload = {
      host: smtpForm.host,
      port: smtpForm.port,
      user: smtpForm.user,
      password: smtpForm.password,
      toAddr: smtpForm.toAddr,
      useSsl: smtpForm.useSsl,
    }
    const cfg = await alertsApi.updateSmtp(payload)
    smtpHasPassword.value = cfg.hasPassword
    smtpMasked.value = cfg.password
    smtpForm.password = ''
    message.success(t('alerts.toast.smtpSaved'))
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    smtpSaving.value = false
  }
}

// ─── SMTP 连通性测试（发一封「欢迎使用 Holdexar」主题邮件）───
const smtpTesting = ref(false)

async function testSmtp() {
  smtpTesting.value = true
  try {
    await alertsApi.testSmtp({
      host: smtpForm.host,
      port: smtpForm.port,
      user: smtpForm.user,
      password: smtpForm.password,
      toAddr: smtpForm.toAddr,
      useSsl: smtpForm.useSsl,
    })
    message.success(t('alerts.toast.testSent', { to: smtpForm.toAddr }))
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    smtpTesting.value = false
  }
}

onMounted(() => {
  load()
  loadSmtp()
  ratesApi
    .list()
    .then((res) => {
      rateMap.value = new Map(res.rates.map((r) => [r.currency, r.rateToCny]))
    })
    .catch(() => {
      /* 汇率不可用只影响换算气泡，页面其余功能不受影响 */
    })
})
</script>

<template>
  <section class="alerts-page">
    <!-- 添加规则 -->
    <div class="card section-card" data-section="alerts.section.rules">
      <div class="section-title">{{ t('alerts.section.rules') }}</div>
      <div class="section-desc">{{ t('alerts.rules.desc') }}</div>

      <!-- 游戏搜索 -->
      <div class="search-row">
        <el-input
          v-model="searchQuery"
          :placeholder="t('alerts.rules.searchPlaceholder')"
          class="search-input"
          :prefix-icon="Search"
          @keyup.enter="searchGames"
          @input="selectedGame = null"
        />
        <el-button :loading="searching" @click="searchGames">{{ t('alerts.rules.search') }}</el-button>
      </div>

      <!-- 搜索结果下拉 -->
      <div v-if="searchResults.length > 0" class="search-results">
        <div
          v-for="game in searchResults"
          :key="game.appid"
          class="search-result-item"
          @click="selectGame(game)"
        >
          <span class="result-name">{{ game.name || game.nameEn }}</span>
          <span class="result-appid">AppID: {{ game.appid }}</span>
        </div>
      </div>
      <div v-if="searchError && searchResults.length === 0" class="search-error">
        {{ searchError }}
      </div>

      <!-- 已选中游戏 -->
      <div v-if="selectedGame" class="selected-game">
        <span class="selected-name">{{ selectedGame.name || selectedGame.nameEn }}</span>
        <span class="selected-appid">AppID: {{ selectedGame.appid }}</span>
        <el-button size="small" text @click="clearSearch">{{ t('alerts.rules.clear') }}</el-button>
      </div>

      <!-- 规则条件 -->
      <div class="add-row">
        <HlSelect v-model="form.region" :options="regionSelectOptions" class="add-row__region" />
        <HlSelect v-model="form.targetType" :options="targetTypeOptions" class="add-row__type" />
        <!-- 外区 price 类输入时悬停出实时换算气泡（约 X.XX 该区货币）；气泡内容为空时 HlTooltip 不弹泡 -->
        <HlTooltip v-if="form.targetType !== 'historic_low'" :content="addConvertHint">
          <el-input v-model="form.targetValue" :placeholder="targetText" class="add-row__value">
            <template #suffix>
              <span class="unit-suffix">{{ form.targetType === 'price' ? t('alerts.rules.unitCny') : '%' }}</span>
            </template>
          </el-input>
        </HlTooltip>
        <span v-else class="section-desc" style="display: inline; margin: 0">{{ targetText }}</span>
        <el-button type="primary" :icon="Plus" @click="add">{{ t('alerts.rules.add') }}</el-button>
      </div>
    </div>

    <!-- 规则列表 -->
    <div class="card section-card" data-section="alerts.section.list">
      <div class="section-title">{{ t('alerts.section.list') }}</div>
      <el-table v-if="alerts.length" :data="alerts" style="width: 100%" size="small" v-loading="loading">
        <el-table-column prop="id" label="#" width="56" />
        <el-table-column :label="t('alerts.table.game')" min-width="240">
          <template #default="{ row }">
            <div class="game-cell">
              <HlImg :src="row.gameHeader" class="game-cover" :alt="row.gameName || String(row.appid)">
                <template #fallback><span class="game-cover game-cover--empty" /></template>
              </HlImg>
              <a class="game-link" @click="router.push(`/game/${row.appid}`)">
                {{ row.gameName || `AppID ${row.appid}` }} ↗
              </a>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="AppID" width="120">
          <template #default="{ row }">
            <span class="appid-text">{{ row.appid }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('alerts.table.region')" width="120">
          <template #default="{ row }">
            <RegionFlag :code="row.region" />
          </template>
        </el-table-column>
        <el-table-column :label="t('alerts.table.condition')" min-width="140">
          <template #default="{ row }">
            <!-- 阈值默认按元展示；外区 price 类悬停显示该区货币换算约价。
                 表格滚动容器裁剪 absolute 气泡，表内气泡走 fixed 定位 -->
            <HlTooltip v-if="row.targetType === 'price'" :content="ruleHoverText(row)" fixed>
              <span class="cond-text">{{ t('alerts.rules.condPrice', { value: formatCnyFen(row.targetValue) }) }}</span>
            </HlTooltip>
            <span v-else-if="row.targetType === 'pct'">{{ t('alerts.rules.condPct', { value: row.targetValue }) }}</span>
            <span v-else>{{ t('alerts.rules.condHistoricLow') }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('alerts.table.enabled')" width="80">
          <template #default="{ row }">
            <el-switch :model-value="row.active" @change="toggle(row)" />
          </template>
        </el-table-column>
        <el-table-column :label="t('alerts.table.lastTriggered')" width="170">
          <template #default="{ row }">
            <span class="muted">{{ row.lastTriggeredAt?.slice(5, 19).replace('T', ' ') ?? t('alerts.rules.never') }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('alerts.table.actions')" width="120" align="right">
          <template #default="{ row }">
            <div class="action-btns">
              <el-button size="small" plain :icon="Edit" @click="startEdit(row)" />
              <el-button size="small" type="danger" plain :icon="Delete" @click="remove(row)" />
            </div>
          </template>
        </el-table-column>
      </el-table>
      <HlEmpty v-else-if="!loading" size="sm" icon="" :text="t('alerts.rules.empty')" />

      <!-- 内联编辑面板（选中编辑时展开） -->
      <div v-if="editingId !== null" class="edit-panel">
        <div class="edit-panel-title">{{ t('alerts.rules.editTitle', { id: editingId ?? '' }) }}</div>
        <div class="edit-row">
          <HlSelect v-model="editForm.targetType" :options="targetTypeOptions" class="edit-select" />
          <HlTooltip v-if="editForm.targetType !== 'historic_low'" :content="editConvertHint">
            <el-input
              v-model="editForm.targetValue"
              size="small"
              :placeholder="t('alerts.rules.targetValuePlaceholder')"
              class="edit-input"
            >
              <template #suffix>
                <span class="unit-suffix">{{ editForm.targetType === 'price' ? t('alerts.rules.unitCny') : '%' }}</span>
              </template>
            </el-input>
          </HlTooltip>
          <HlSelect v-model="editForm.region" :options="regionSelectOptions" class="edit-region" />
          <el-button size="small" type="primary" @click="saveEdit(alerts.find(a => a.id === editingId)!)">{{ t('alerts.rules.save') }}</el-button>
          <el-button size="small" @click="cancelEdit">{{ t('common.cancel') }}</el-button>
        </div>
      </div>
    </div>

    <!-- 触发历史 -->
    <div class="card section-card" data-section="alerts.section.history">
      <div class="history-head">
        <div class="section-title">{{ t('alerts.section.history') }}</div>
        <el-button
          v-if="events.length"
          size="small"
          type="danger"
          plain
          :icon="Delete"
          @click="clearAllEvents"
        >
          {{ t('alerts.history.clearAll') }}
        </el-button>
      </div>
      <el-table v-if="events.length" :data="events" style="width: 100%" size="small" max-height="280">
        <el-table-column :label="t('alerts.table.time')" width="160">
          <template #default="{ row }">{{ row.triggeredAt?.slice(5, 19).replace('T', ' ') }}</template>
        </el-table-column>
        <el-table-column :label="t('alerts.table.game')" min-width="230">
          <template #default="{ row }">
            <div class="game-cell">
              <HlImg :src="row.gameHeader" class="game-cover" :alt="row.gameName || String(row.appid)">
                <template #fallback><span class="game-cover game-cover--empty" /></template>
              </HlImg>
              <a class="game-link" @click="router.push(`/game/${row.appid}`)">
                {{ row.gameName || `AppID ${row.appid}` }} ↗
              </a>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="AppID" width="110">
          <template #default="{ row }">
            <span class="appid-text">{{ row.appid }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('alerts.table.region')" width="110">
          <template #default="{ row }">
            <RegionFlag :code="row.region" />
          </template>
        </el-table-column>
        <el-table-column :label="t('alerts.table.priceThen')" width="130">
          <template #default="{ row }">
            <!-- 人民币快照优先；外区行悬停看触发时的本币原价 -->
            <HlTooltip :content="eventHoverText(row)" fixed>
              <span class="hl-num">{{ eventPriceText(row) }}</span>
            </HlTooltip>
          </template>
        </el-table-column>
        <el-table-column :label="t('alerts.table.email')" width="80">
          <template #default="{ row }">
            <span class="tag" :class="row.notified ? 'tag--success' : ''">
              {{ row.notified ? t('alerts.history.notifiedEmail') : t('alerts.history.notifiedInApp') }}
            </span>
          </template>
        </el-table-column>
        <el-table-column :label="t('alerts.table.actions')" width="70" align="right">
          <template #default="{ row }">
            <el-button size="small" type="danger" plain :icon="Delete" @click="removeEvent(row)" />
          </template>
        </el-table-column>
      </el-table>
      <HlEmpty v-else size="sm" icon="" :text="t('alerts.history.empty')" />
    </div>

    <!-- 邮件设置 -->
    <div class="card section-card" v-loading="smtpLoading" data-section="alerts.section.smtp">
      <div class="section-title">{{ t('alerts.section.smtp') }}</div>
      <div class="section-desc">{{ t('alerts.smtp.desc') }}</div>
      <el-form label-position="top" class="smtp-form">
        <div class="smtp-grid">
          <!-- 两条示例占位符的示例值（服务器域名 / 邮箱）留在视图侧传入，
               词典里只有句式 {host} / {email}——连接信息不进词条 -->
          <el-form-item :label="t('alerts.smtp.host')">
            <el-input
              v-model="smtpForm.host"
              :placeholder="t('alerts.smtp.hostPlaceholder', { host: 'smtp.qq.com' })"
            />
          </el-form-item>
          <el-form-item :label="t('alerts.smtp.port')">
            <el-input-number v-model="smtpForm.port" :min="1" :max="65535" controls-position="right" />
          </el-form-item>
          <el-form-item :label="t('alerts.smtp.user')">
            <el-input
              v-model="smtpForm.user"
              :placeholder="t('alerts.smtp.userPlaceholder', { email: 'your@qq.com' })"
            />
          </el-form-item>
          <el-form-item :label="t('alerts.smtp.password')">
            <el-input
              v-model="smtpForm.password"
              type="password"
              show-password
              :placeholder="
                smtpHasPassword
                  ? t('alerts.smtp.passwordConfigured', { masked: smtpMasked })
                  : t('alerts.smtp.passwordPlaceholder')
              "
            />
          </el-form-item>
          <el-form-item :label="t('alerts.smtp.toAddr')">
            <el-input
              v-model="smtpForm.toAddr"
              :placeholder="t('alerts.smtp.toAddrPlaceholder')"
            />
          </el-form-item>
          <el-form-item :label="t('alerts.smtp.encryption')">
            <el-switch
              v-model="smtpForm.useSsl"
              :active-text="t('alerts.smtp.ssl')"
              :inactive-text="t('alerts.smtp.starttls')"
            />
          </el-form-item>
        </div>
      </el-form>
      <div class="smtp-footer">
        <el-button :loading="smtpTesting" @click="testSmtp">{{ t('alerts.smtp.test') }}</el-button>
        <el-button type="primary" :loading="smtpSaving" @click="saveSmtp">{{ t('alerts.smtp.save') }}</el-button>
      </div>
    </div>
  </section>
</template>

<style scoped>
.alerts-page {
  max-width: 960px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.section-card {
  padding: 20px 24px;
}

.search-row {
  display: flex;
  gap: 8px;
  margin-top: 12px;
}

.search-input {
  flex: 1;
}

.search-results {
  margin-top: 8px;
  border: 1px solid var(--border-soft);
  border-radius: var(--radius);
  max-height: 240px;
  overflow-y: auto;
  background: var(--bg-card);
}

.search-result-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 12px;
  cursor: pointer;
  transition: background var(--transition);
  border-bottom: 1px solid var(--border-soft);
}

.search-result-item:last-child {
  border-bottom: none;
}

.search-result-item:hover {
  background: var(--accent-soft);
}

.result-name {
  font-size: 13px;
  color: var(--text-primary);
}

.result-appid {
  font-size: 11px;
  color: var(--text-muted);
  font-family: var(--font-mono);
}

.search-error {
  margin-top: 8px;
  padding: 8px 12px;
  border: 1px solid var(--danger-a30);
  border-radius: var(--radius);
  background: var(--danger-a15);
  color: var(--danger);
  font-size: 12px;
}

.selected-game {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
  padding: 8px 12px;
  border: 1px solid var(--accent-a30);
  border-radius: var(--radius);
  background: var(--accent-soft);
}

.selected-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--accent);
}

.selected-appid {
  font-size: 11px;
  color: var(--text-muted);
  font-family: var(--font-mono);
}

.add-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
  flex-wrap: wrap;
}

.add-row__region {
  /* ≥ .hl-select-wrap 的 min-width:170px——宿主比 wrap 窄会溢出压到相邻控件 */
  width: 170px;
}

.add-row__type {
  width: 170px;
}

.add-row__value {
  width: 160px;
}

.appid-text {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-muted);
}

/* 游戏列：封面 + 名称链接（规则表 / 触发历史表共用） */
.game-cell {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.game-cover {
  width: 92px;
  height: 43px;
  flex: none;
  border-radius: 4px;
  object-fit: cover;
  display: block;
  background: var(--accent-soft);
}

.game-cover--empty {
  border: 1px solid var(--border-soft);
}

/* 金额输入框后缀单位（元 / %） */
.unit-suffix {
  font-size: 12px;
  color: var(--text-muted);
}

.cond-text {
  cursor: default;
}

.history-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.muted {
  font-size: 12px;
  color: var(--text-muted);
}

.game-link {
  color: var(--accent);
  cursor: pointer;
  font-size: 12.5px;
}

.game-link:hover {
  text-decoration: underline;
}

.action-btns {
  display: flex;
  gap: 6px;
  justify-content: flex-end;
}

.edit-panel {
  margin-top: 12px;
  padding: 12px 16px;
  border: 1px solid var(--accent);
  border-radius: var(--radius);
  background: var(--accent-soft);
}

.edit-panel-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--accent);
  margin-bottom: 8px;
}

.edit-row {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}

.edit-select {
  width: 170px;
}

.edit-input {
  width: 120px;
}

.edit-region {
  width: 170px;
}

.smtp-form {
  margin-top: 12px;
}

.smtp-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 0 16px;
}

.smtp-grid .el-form-item {
  margin-bottom: 12px;
}

.smtp-footer {
  display: flex;
  justify-content: flex-end;
  margin-top: 8px;
}
</style>
