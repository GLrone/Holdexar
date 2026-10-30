<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { alertsApi, notificationsApi, type AlertEventItem, type FeaturedConfig, type NotificationCandidate, type NotificationCondition, type NotificationFrequency, type NotificationPrefs, type NotificationPrefsUpdate, type NotificationStats, type PriceAlertDraft, type PriceAlertItem, type RuleEvaluation, type GameSearchResult } from '@/api/client'
import { formatMinor } from '@/api/currencies'
import { regionSelectOptions } from '@/api/selectOptions'
import { amountToMinor, minorToInput } from '@/lib/notificationForms'
import { useI18n, type MessageKey } from '@/locales'
import { useRegionsStore } from '@/stores/regions'
import RegionFlag from '@/components/RegionFlag.vue'
import CurrencyFlag from '@/components/CurrencyFlag.vue'
import { HlButton, HlDialog, HlEmpty, HlInput, HlSelect, HlTabs, message } from '@/components/ui'

const { t } = useI18n()
const c = (key: string, params?: Record<string, string | number>) => t(`alerts.center.${key}` as MessageKey, params)
const regions = useRegionsStore()
const tab = ref('overview')
const tabs = computed(() => ['overview', 'types', 'rules', 'history'].map(key => ({ key, label: c(key) })))
const loading = ref(true)
const busy = ref(false)
const error = ref('')
const prefs = ref<NotificationPrefs | null>(null)
const stats = ref<NotificationStats | null>(null)
const rules = ref<PriceAlertItem[]>([])
const candidates = ref<NotificationCandidate[]>([])
const legacyEvents = ref<AlertEventItem[]>([])
const manualRegion = ref('')
const regionOptions = computed(() => [{ value: '', label: c('unknownRegion') }, ...regionSelectOptions(undefined, { withCurrency: true })])
const windows = computed(() => [0, 90, 180, 365].map(value => ({ value, label: value ? c('windowDays', { days: value }) : c('allHistory') })))
const frequencies = computed(() => ['immediate', 'daily', 'weekly', 'off'].map(value => ({ value, label: c(`frequency.${value}`) })))
const ruleTypes = computed(() => ['price', 'pct', 'historic_low', 'new_low'].map(value => ({ value, label: c(`ruleType.${value}`) })))
const repeats = computed(() => ['once', 'cooldown', 'always'].map(value => ({ value, label: c(`repeat.${value}`) })))
const effectiveCurrency = computed(() => prefs.value?.region.currency || '')
const latestMatch = computed(() => candidates.value.map(item => item.createdAt).filter((value): value is string => !!value).sort().at(-1))
const dateText = (value?: string | null) => value ? value.slice(0, 19).replace('T', ' ') : c('noRecord')
function typeLabel(key: string) {
  const translated = c(`type.${key}`)
  return translated.startsWith('alerts.center.') ? c('otherType') : translated
}
function statusLabel(key: string) {
  const translated = c(`status.${key}`)
  return translated.startsWith('alerts.center.') ? c('status.unknown') : translated
}
function priceText(price?: number | null, currency?: string | null) {
  return price != null && currency ? `${formatMinor(price, currency)} (${currency})` : c('unconfirmedPrice')
}
function fail(cause: unknown) {
  error.value = cause instanceof Error ? cause.message : String(cause)
  message.error(error.value)
}
async function load() {
  loading.value = true
  error.value = ''
  try {
    const [config, counts, saved, history, old] = await Promise.all([
      notificationsApi.prefs(), notificationsApi.stats(), alertsApi.list(), notificationsApi.candidates(), alertsApi.events(50), regions.load(),
    ])
    prefs.value = config
    stats.value = counts
    rules.value = saved
    candidates.value = history.items
    legacyEvents.value = old
    manualRegion.value = config.manualRegion?.toLowerCase() || ''
    if (!draft.region && config.region.region) draft.region = config.region.region.toLowerCase()
    resetFeatured()
  } catch (cause) { fail(cause) }
  finally { loading.value = false }
}
async function patch(payload: NotificationPrefsUpdate) {
  busy.value = true
  try { prefs.value = await notificationsApi.updatePrefs(payload); message.success(c('saved')) }
  catch (cause) { fail(cause) }
  finally { busy.value = false }
}
async function setType(key: string, value: string | number | Array<string | number>) {
  await patch({ types: { [key]: { frequency: String(value) as NotificationFrequency, enabled: value !== 'off' } } })
}

const editingId = ref<number | null>(null)
const draft = reactive({ appid: 0, region: '', targetType: 'price', amount: '', windowDays: 0, legacy: false, repeatMode: 'once' as 'once' | 'cooldown' | 'always', repeatHours: '24' })
const search = ref('')
const results = ref<GameSearchResult[]>([])
const searching = ref(false)
const draftCurrency = computed(() => draft.legacy ? 'CNY' : regions.metaByCode(draft.region)?.currency || '')
const previewItems = ref<RuleEvaluation[]>([])
const previewTotal = ref(0)
const previewMatched = ref(0)
const previewBusy = ref(false)
const previewError = ref('')
const previewKind = ref('')
const previewStale = ref(false)
watch(draft, () => { previewStale.value = true })
async function searchGames() {
  searching.value = true
  try { const found = await alertsApi.search(search.value, draft.region || undefined); results.value = found.items; if (found.error) message.warning(found.error) }
  catch (cause) { fail(cause) }
  finally { searching.value = false }
}
function chooseGame(game: GameSearchResult) { draft.appid = game.appid; search.value = game.name || String(game.appid); results.value = [] }
function edit(rule: PriceAlertItem) {
  editingId.value = rule.id
  search.value = rule.gameName
  Object.assign(draft, { appid: rule.appid, region: rule.region.toLowerCase(), targetType: rule.targetType, amount: rule.targetType === 'price' ? minorToInput(rule.targetValue) : String(rule.targetValue ?? ''), windowDays: rule.config?.windowDays || 0, legacy: rule.targetType === 'price' && !rule.config?.currency, repeatMode: rule.repeatMode || 'once', repeatHours: String(rule.repeatHours || 24) })
  tab.value = 'rules'
}
function resetDraft() {
  editingId.value = null
  search.value = ''
  results.value = []
  Object.assign(draft, { appid: 0, region: prefs.value?.region.region?.toLowerCase() || '', targetType: 'price', amount: '', windowDays: 0, legacy: false, repeatMode: 'once', repeatHours: '24' })
  previewItems.value = []
}
function rulePayload(): PriceAlertDraft {
  if (!draft.appid || !draft.region) throw new Error(c('chooseGameRegion'))
  const payload: PriceAlertDraft = { appid: draft.appid, region: draft.region.toUpperCase(), targetType: draft.targetType, repeatMode: draft.repeatMode, repeatHours: Number(draft.repeatHours), config: { windowDays: draft.windowDays || null } }
  if (draft.targetType === 'price') {
    if (!draftCurrency.value) throw new Error(c('unknownCurrency'))
    const amount = amountToMinor(draft.amount)
    if (amount === null) throw new Error(c('invalidAmount'))
    payload.targetValue = amount
    if (!draft.legacy) payload.config!.currency = draftCurrency.value
  } else if (draft.targetType === 'pct') {
    const amount = Number(draft.amount)
    if (!draft.amount.trim() || !Number.isInteger(amount) || amount < 0 || amount > 100) throw new Error(c('invalidPercent'))
    payload.targetValue = amount
  }
  return payload
}
async function saveRule() {
  busy.value = true
  try {
    const payload = rulePayload()
    if (editingId.value !== null) await alertsApi.update(editingId.value, payload)
    else await alertsApi.add(payload.appid, payload.region, payload.targetType, payload.targetValue, payload)
    rules.value = await alertsApi.list()
    message.success(c('saved'))
    resetDraft()
  } catch (cause) { fail(cause) }
  finally { busy.value = false }
}
async function toggleRule(rule: PriceAlertItem) {
  busy.value = true
  try { const updated = await alertsApi.update(rule.id, { active: !rule.active }); rules.value = rules.value.map(item => item.id === updated.id ? updated : item) }
  catch (cause) { fail(cause) }
  finally { busy.value = false }
}
const deleting = ref<PriceAlertItem | null>(null)
const deleteOpen = computed({ get: () => deleting.value !== null, set: value => { if (!value) deleting.value = null } })
async function removeRule() {
  if (!deleting.value) return
  busy.value = true
  try { await alertsApi.remove(deleting.value.id); rules.value = rules.value.filter(item => item.id !== deleting.value!.id); deleting.value = null }
  catch (cause) { fail(cause) }
  finally { busy.value = false }
}
function ruleCondition(rule: PriceAlertItem) {
  if (rule.targetType === 'price') return `${c('ruleType.price')} ${priceText(rule.targetValue, rule.config?.currency || 'CNY')}${!rule.config?.currency ? ` · ${c('legacyAmount')}` : ''}`
  if (rule.targetType === 'pct') return `${c('ruleType.pct')} ${rule.targetValue}%`
  return `${c(`ruleType.${rule.targetType}`)} · ${rule.config?.windowDays ? c('windowDays', { days: rule.config.windowDays }) : c('allHistory')}`
}

const featured = ref<FeaturedConfig | null>(null)
const featuredAmounts = reactive({ minSavings: '', maxPrice: '', minPositiveRate: '', minReviews: '' })
const scopes = ['followed', 'wishlist', 'owned', 'manual', 'catalog']
const sorts = ['discount', 'price', 'savings', 'quality', 'followed', 'wishlist']
function resetFeatured() {
  if (!prefs.value?.featured) return
  featured.value = { ...prefs.value.featured, scope: [...prefs.value.featured.scope] }
  featuredAmounts.minSavings = minorToInput(featured.value.minSavings)
  featuredAmounts.maxPrice = minorToInput(featured.value.maxPrice)
  featuredAmounts.minPositiveRate = String(featured.value.minPositiveRate ?? '')
  featuredAmounts.minReviews = String(featured.value.minReviews ?? '')
}
watch([featured, featuredAmounts], () => { if (previewKind.value === 'featured') previewStale.value = true }, { deep: true })
function featuredPayload(): FeaturedConfig {
  if (!featured.value) throw new Error(c('loadFailed'))
  const config = { ...featured.value, scope: [...featured.value.scope] }
  for (const key of ['minSavings', 'maxPrice'] as const) {
    const input = featuredAmounts[key].trim()
    config[key] = input ? amountToMinor(input) : null
    if (input && (config[key] === null || !effectiveCurrency.value)) throw new Error(c('invalidAmount'))
  }
  for (const key of ['minPositiveRate', 'minReviews'] as const) {
    const input = featuredAmounts[key].trim()
    const value = Number(input)
    if (input && (!Number.isSafeInteger(value) || value < 0 || (key === 'minPositiveRate' && value > 100))) throw new Error(c('invalidNumber'))
    config[key] = input ? value : null
  }
  return config
}
function toggleScope(scope: string) {
  if (!featured.value) return
  featured.value.scope = featured.value.scope.includes(scope) ? featured.value.scope.filter(item => item !== scope) : [...featured.value.scope, scope]
}
async function saveFeatured() {
  try { await patch({ featured: featuredPayload() }) }
  catch (cause) { fail(cause) }
}
async function preview(kind: 'rule' | 'featured') {
  previewBusy.value = true
  previewError.value = ''
  previewKind.value = kind
  try {
    if (kind === 'rule') { const result = await alertsApi.preview(rulePayload()); previewItems.value = [result]; previewTotal.value = 1; previewMatched.value = Number(result.matched) }
    else { const result = await alertsApi.featuredPreview(featuredPayload()); previewItems.value = result.items; previewTotal.value = result.total; previewMatched.value = result.matched; if (result.exclusions?.length) previewError.value = c('unknownRegion') }
    previewStale.value = false
  } catch (cause) { previewError.value = cause instanceof Error ? cause.message : String(cause); previewItems.value = [] }
  finally { previewBusy.value = false }
}
function conditionLabel(condition: NotificationCondition) {
  const value = c(`condition.${condition.code}`)
  return value.startsWith('alerts.center.') ? c('condition.other') : value
}
function conditionNumbers(condition: NotificationCondition, evaluation: RuleEvaluation) {
  const money = ['target_price', 'min_savings', 'max_price', 'low', 'new_low', 'no_baseline'].includes(condition.code)
  const currency = condition.code === 'target_price' && evaluation.observed.thresholdBasis === 'legacy_cny' ? 'CNY' : evaluation.observed.currency
  const actual = typeof condition.actual === 'number' ? (money ? priceText(condition.actual, currency) : String(condition.actual)) : ''
  const expected = typeof condition.expected === 'number' ? (money ? priceText(condition.expected, currency) : String(condition.expected)) : ''
  return actual || expected ? c('actualExpected', { actual: actual || c('missing'), expected: expected || c('missing') }) : ''
}
function candidatePrice(item: NotificationCandidate) { const value = item.payload; return priceText(value.price ?? value.current?.price, value.currency ?? value.current?.currency) }
const historyPreview = ref<NotificationCandidate | null>(null)
const historyOpen = computed({ get: () => historyPreview.value !== null, set: value => { if (!value) historyPreview.value = null } })
onMounted(load)
</script>

<template>
  <section class="alerts-page" :aria-busy="loading">
    <div class="page-actions"><HlButton size="sm" :loading="loading" @click="load">{{ t('common.refresh') }}</HlButton><router-link to="/settings?s=notification">{{ c('mailSettings') }}</router-link></div>
    <p v-if="error" class="notice is-error" role="alert">{{ error }}</p>
    <p v-if="loading">{{ t('common.loading') }}</p>
    <HlTabs v-model="tab" :tabs="tabs">
      <div v-if="tab === 'overview'" class="card section-card">
        <h2>{{ c('overview') }}</h2>
        <div class="summary-grid">
          <div><span>{{ c('notificationSwitch') }}</span><strong>{{ prefs?.enabled ? c('enabled') : c('disabled') }}</strong></div>
          <div><span>{{ c('mailConfig') }}</span><strong>{{ prefs?.smtp.complete ? c('complete') : prefs?.smtp.configured ? c('incomplete') : c('notConfigured') }}</strong></div>
          <div><span>{{ c('defaultRegion') }}</span><strong><RegionFlag v-if="prefs?.region.region" :code="prefs.region.region" /><template v-else>{{ c('unknownRegion') }}</template></strong></div>
          <div><span>{{ c('currency') }}</span><strong><CurrencyFlag v-if="effectiveCurrency" :code="effectiveCurrency" /><template v-else>{{ c('unknownCurrency') }}</template></strong></div>
          <div><span>{{ c('ruleCount') }}</span><strong>{{ rules.length }}</strong></div>
          <div><span>{{ c('pendingCount') }}</span><strong>{{ (stats?.pending || 0) + (stats?.suppressed || 0) }}</strong></div>
          <div><span>{{ c('latestMatch') }}</span><strong>{{ dateText(latestMatch) }}</strong></div>
          <div><span>{{ c('lastDelivery') }}</span><strong>{{ prefs?.lastDelivery ? statusLabel(prefs.lastDelivery.status) : c('noRecord') }}</strong><small>{{ dateText(prefs?.lastDelivery?.at) }}</small></div>
        </div>
        <p v-if="!prefs?.region.region" class="notice">{{ c('chooseRegionHint') }}</p>
        <div class="form-grid">
          <label class="field"><span>{{ c('manualRegion') }}</span><HlSelect v-model="manualRegion" :options="regionOptions" searchable /></label>
          <div class="field action-end"><HlButton :disabled="busy || !prefs" @click="patch({ manualRegion: manualRegion || null })">{{ t('common.save') }}</HlButton></div>
        </div>
        <div class="actions"><HlButton :disabled="busy || !prefs" :aria-pressed="!!prefs?.enabled" @click="patch({ enabled: !prefs?.enabled })">{{ prefs?.enabled ? c('disableMail') : c('enableMail') }}</HlButton></div>
        <p class="muted">{{ c('acceptedHint') }}</p>
      </div>

      <div v-if="tab === 'types'" class="section-stack">
        <div class="card section-card">
          <h2>{{ c('types') }}</h2><p class="muted">{{ c('frequencyHint', { time: prefs?.digestTime || '08:00' }) }}</p>
          <label class="field compact-field"><span>{{ c('weeklyDay') }}</span><HlSelect :model-value="prefs?.digestWeekday ?? 0" :disabled="busy" :options="[0,1,2,3,4,5,6].map(value => ({ value, label: c(`weekday.${value}`) }))" @update:model-value="value => patch({ digestWeekday: Number(value) })" /></label>
          <div v-for="item in prefs?.types || []" :key="item.key" class="type-row"><div><strong>{{ typeLabel(item.key) }}</strong><p class="muted">{{ c(`purpose.${item.category}`) }}</p></div><HlSelect :model-value="item.enabled ? item.frequency : 'off'" :options="frequencies" :disabled="busy" @update:model-value="value => setType(item.key, value)" /></div>
        </div>
        <div v-if="featured" class="card section-card">
          <h2>{{ c('featured') }}</h2><p class="muted">{{ c('featuredHint') }}</p>
          <div class="actions"><HlButton v-for="scope in scopes" :key="scope" size="sm" :variant="featured.scope.includes(scope) ? 'primary' : 'default'" :aria-pressed="featured.scope.includes(scope)" @click="toggleScope(scope)">{{ c(`scope.${scope}`) }}</HlButton></div>
          <div class="form-grid">
            <label class="field"><span>{{ c('minDiscount') }}</span><HlInput :model-value="String(featured.minDiscount ?? '')" type="number" @update:model-value="value => featured && (featured.minDiscount = value === '' ? null : Number(value))" /></label>
            <label class="field"><span>{{ c('minSavings') }} <CurrencyFlag v-if="effectiveCurrency" :code="effectiveCurrency" /></span><HlInput v-model="featuredAmounts.minSavings" inputmode="decimal" /></label>
            <label class="field"><span>{{ c('maxPrice') }} <CurrencyFlag v-if="effectiveCurrency" :code="effectiveCurrency" /></span><HlInput v-model="featuredAmounts.maxPrice" inputmode="decimal" /></label>
            <label class="field"><span>{{ c('lowRequirement') }}</span><HlSelect v-model="featured.lowMode" :options="['any','low','new_low'].map(value => ({ value, label: c(`lowMode.${value}`) }))" /></label>
            <label class="field"><span>{{ c('window') }}</span><HlSelect :model-value="featured.windowDays || 0" :options="windows" @update:model-value="value => featured && (featured.windowDays = Number(value) || null)" /></label>
            <label class="field"><span>{{ c('sort') }}</span><HlSelect v-model="featured.sort" :options="sorts.map(value => ({ value, label: c(`sort.${value}`) }))" /></label>
          </div>
          <details><summary>{{ c('advanced') }}</summary><div class="form-grid">
            <label class="field"><span>{{ c('minPositiveRate') }}</span><HlInput v-model="featuredAmounts.minPositiveRate" type="number" /></label>
            <label class="field"><span>{{ c('minReviews') }}</span><HlInput v-model="featuredAmounts.minReviews" type="number" /></label>
            <label class="field"><span>{{ c('missingReviews') }}</span><HlSelect v-model="featured.missingReviews" :options="['include','exclude'].map(value => ({ value, label: c(value) }))" /></label>
          </div><div class="actions"><HlButton v-for="key in (['allowFullPrice','includeOwned','includeIgnored'] as const)" :key="key" size="sm" :aria-pressed="featured[key]" :variant="featured[key] ? 'primary' : 'default'" @click="featured[key] = !featured[key]">{{ c(key) }}</HlButton></div></details>
          <div class="actions"><HlButton :loading="previewBusy" @click="preview('featured')">{{ c('previewDraft') }}</HlButton><HlButton variant="primary" :loading="busy" @click="saveFeatured">{{ t('common.save') }}</HlButton><HlButton @click="resetFeatured">{{ c('restoreSaved') }}</HlButton></div>
        </div>
      </div>

      <div v-if="tab === 'rules'" class="section-stack">
        <div class="card section-card" data-section="alerts.section.rules">
          <h2>{{ editingId === null ? t('alerts.section.rules') : t('alerts.rules.editTitle', { id: editingId }) }}</h2>
          <form @submit.prevent="searchGames"><label class="field"><span>{{ t('alerts.table.game') }}</span><div class="search-row"><HlInput v-model="search" :placeholder="t('alerts.rules.searchPlaceholder')" /><HlButton type="submit" :loading="searching">{{ t('alerts.rules.search') }}</HlButton></div></label></form>
          <div v-if="results.length" class="search-results"><HlButton v-for="game in results" :key="game.appid" variant="text" block @click="chooseGame(game)">{{ game.name }} · {{ game.appid }}</HlButton></div>
          <p v-if="draft.appid" class="muted">{{ c('selectedGame', { name: search, id: draft.appid }) }}</p>
          <div class="form-grid">
            <label class="field"><span>{{ c('region') }}</span><HlSelect v-model="draft.region" :options="regionOptions" searchable /></label>
            <label class="field"><span>{{ c('conditionTitle') }}</span><HlSelect v-model="draft.targetType" :options="ruleTypes" /></label>
            <label v-if="draft.targetType === 'price' || draft.targetType === 'pct'" class="field"><span>{{ c('target') }} <CurrencyFlag v-if="draft.targetType === 'price' && draftCurrency" :code="draftCurrency" /><template v-if="draft.targetType === 'pct'">%</template></span><HlInput v-model="draft.amount" inputmode="decimal" /></label>
            <label v-if="draft.targetType === 'historic_low' || draft.targetType === 'new_low'" class="field"><span>{{ c('window') }}</span><HlSelect v-model="draft.windowDays" :options="windows" /></label>
            <label class="field"><span>{{ c('repeatTitle') }}</span><HlSelect v-model="draft.repeatMode" :options="repeats" /></label>
            <label v-if="draft.repeatMode === 'cooldown'" class="field"><span>{{ c('repeatHours') }}</span><HlInput v-model="draft.repeatHours" type="number" /></label>
          </div>
          <p v-if="draft.legacy && draft.targetType === 'price'" class="notice">{{ c('legacyHint') }}</p>
          <p v-else-if="draft.targetType === 'price' && !draftCurrency" class="notice">{{ c('unknownCurrency') }}</p>
          <div class="actions"><HlButton :loading="previewBusy" @click="preview('rule')">{{ c('previewDraft') }}</HlButton><HlButton variant="primary" :loading="busy" @click="saveRule">{{ editingId === null ? t('alerts.rules.add') : t('common.save') }}</HlButton><HlButton @click="resetDraft">{{ t('common.cancel') }}</HlButton></div>
        </div>
        <div class="card section-card"><h2>{{ c('rules') }}</h2><HlEmpty v-if="!rules.length && !loading" :text="t('alerts.rules.empty')" />
          <article v-for="rule in rules" :key="rule.id" class="record-row"><div><router-link :to="`/game/${rule.appid}`">{{ rule.gameName || rule.appid }}</router-link><p><RegionFlag :code="rule.region" /> · {{ ruleCondition(rule) }}</p><small>{{ c('latestMatch') }}: {{ dateText(rule.lastTriggeredAt) }}</small></div><div class="actions"><HlButton size="sm" :disabled="busy" :aria-pressed="rule.active" @click="toggleRule(rule)">{{ rule.active ? c('enabled') : c('disabled') }}</HlButton><HlButton size="sm" @click="edit(rule)">{{ c('edit') }}</HlButton><HlButton size="sm" variant="text" @click="deleting = rule">{{ c('delete') }}</HlButton></div></article>
        </div>
      </div>

      <div v-if="tab === 'history'" class="section-stack">
        <div class="card section-card"><h2>{{ c('history') }}</h2><p class="muted">{{ c('acceptedHint') }}</p><HlEmpty v-if="!candidates.length && !loading" :text="c('historyEmpty')" />
          <article v-for="item in candidates" :key="item.id" class="record-row"><div><strong>{{ item.payload.gameName || typeLabel(item.eventType) }}</strong><p>{{ typeLabel(item.eventType) }} · {{ statusLabel(item.status) }}</p><p><RegionFlag v-if="item.region" :code="item.region" /> {{ candidatePrice(item) }}</p><small>{{ c('matchedAt') }}: {{ dateText(item.createdAt) }} · {{ c('earliestSend') }}: {{ dateText(item.dueAt) }}</small><p v-if="item.reason" class="muted">{{ item.reason }}</p></div><HlButton size="sm" @click="historyPreview = item">{{ c('viewContent') }}</HlButton></article>
        </div>
        <details v-if="legacyEvents.length" class="card section-card"><summary>{{ c('legacyHistory') }}</summary><article v-for="item in legacyEvents" :key="item.id" class="record-row"><div><router-link :to="`/game/${item.appid}`">{{ item.gameName || item.appid }}</router-link><p><RegionFlag :code="item.region" /> · {{ item.priceText || c('unconfirmedPrice') }}</p><small>{{ dateText(item.triggeredAt) }} · {{ item.notified ? c('status.delivered') : c('legacyNoReceipt') }}</small></div></article></details>
      </div>
    </HlTabs>

    <section v-if="previewKind && ((tab === 'rules' && previewKind === 'rule') || (tab === 'types' && previewKind === 'featured'))" class="card section-card" aria-live="polite">
      <h2>{{ c('previewTitle') }}</h2><p class="muted">{{ c('previewHint') }}</p><p v-if="previewStale" class="notice">{{ c('previewStale') }}</p><p v-if="previewBusy">{{ t('common.loading') }}</p><p v-else>{{ c('previewCount', { matched: previewMatched, total: previewTotal }) }}</p><p v-if="previewError" role="alert">{{ previewError }}</p>
      <article v-for="(item, index) in previewItems" :key="`${item.observed.appid}-${index}`" class="preview-row"><h3>{{ item.observed.gameName || item.observed.appid }} · {{ item.matched ? c('matched') : c('excluded') }}</h3><p><RegionFlag v-if="item.observed.region" :code="item.observed.region" /> · {{ priceText(item.observed.price, item.observed.currency) }}</p><p class="muted">{{ c('observedAt') }}: {{ dateText(item.observed.observedAt) }}</p><ul class="condition-list"><li v-for="condition in item.conditions" :key="condition.code" :class="{ 'is-error': !condition.passed }">{{ condition.passed ? c('passed') : c('notPassed') }} · {{ conditionLabel(condition) }} <small>{{ conditionNumbers(condition, item) }}</small></li></ul><p v-if="item.comparison.points != null">{{ c('baseline', { price: priceText(item.comparison.baselineLow, item.observed.currency), points: item.comparison.points }) }}</p><p v-if="item.comparison.firstAt" class="muted">{{ dateText(item.comparison.firstAt) }} — {{ dateText(item.comparison.lastAt) }}</p><p v-if="item.comparison.points != null" class="muted">{{ c('coverageHint') }}</p></article>
    </section>
    <HlDialog v-model="deleteOpen" :title="c('delete')"><p>{{ t('alerts.rules.removeConfirm', { id: deleting?.id || '', name: deleting?.gameName || '' }) }}</p><template #footer><HlButton @click="deleteOpen = false">{{ t('common.cancel') }}</HlButton><HlButton variant="danger" :loading="busy" @click="removeRule">{{ c('delete') }}</HlButton></template></HlDialog>
    <HlDialog v-model="historyOpen" :title="c('viewContent')"><template v-if="historyPreview"><h3>{{ historyPreview.payload.gameName || typeLabel(historyPreview.eventType) }}</h3><p>{{ candidatePrice(historyPreview) }}</p><p>{{ statusLabel(historyPreview.status) }} · {{ dateText(historyPreview.deliveredAt) }}</p><p>{{ c('observedAt') }}: {{ dateText(historyPreview.payload.observedAt || historyPreview.payload.current?.observedAt) }}</p><ul v-if="historyPreview.payload.evaluation"><li v-for="condition in historyPreview.payload.evaluation.conditions" :key="condition.code">{{ conditionLabel(condition) }} · {{ condition.passed ? c('passed') : c('notPassed') }}</li></ul><p v-if="historyPreview.reason">{{ historyPreview.reason }}</p></template></HlDialog>
  </section>
</template>

<style scoped>
.alerts-page { max-width: 1080px; margin: 0 auto; display: flex; flex-direction: column; gap: 16px; }
.page-actions, .actions, .search-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.page-actions { justify-content: space-between; }
.section-stack { display: grid; gap: 16px; }
.section-card { padding: 22px; min-width: 0; }
h2 { margin: 0 0 16px; font-size: 17px; } h3 { margin: 8px 0; font-size: 14px; }
p { line-height: 1.7; margin: 8px 0; } small, .muted { color: var(--text-muted); font-size: 12px; }
.summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(195px, 1fr)); gap: 20px; margin-bottom: 24px; }
.summary-grid > div { display: flex; flex-direction: column; gap: 8px; } .summary-grid span { color: var(--text-muted); font-size: 12px; } .summary-grid strong { font-size: 14px; overflow-wrap: anywhere; }
.form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin: 18px 0; }
.field { display: flex; flex-direction: column; gap: 8px; min-width: 0; font-size: 12px; color: var(--text-muted); }
.field > span { display: flex; align-items: center; gap: 8px; } .compact-field { max-width: 250px; margin-bottom: 20px; } .action-end { justify-content: end; align-items: start; }
.actions { margin: 12px 0; } .search-row { flex-wrap: nowrap; } .search-row > :first-child { flex: 1; min-width: 0; }
.search-results { max-height: 220px; overflow-y: auto; border: 1px solid var(--border-soft); border-radius: var(--radius); }
.type-row, .record-row { display: flex; justify-content: space-between; align-items: center; gap: 18px; padding: 16px 0; border-bottom: 1px solid var(--border-soft); }
.type-row > :last-child { min-width: 175px; } .record-row > div { min-width: 0; } .record-row p { overflow-wrap: anywhere; } .record-row .actions { flex-shrink: 0; }
.notice { padding: 12px; background: var(--accent-soft); border-radius: var(--radius); } .is-error { color: var(--danger); }
.preview-row { padding: 16px 0; border-top: 1px solid var(--border-soft); } .condition-list { padding-left: 20px; line-height: 1.9; } .condition-list small { margin-left: 6px; }
summary { cursor: pointer; padding: 8px 0; font-weight: 600; } a { color: var(--accent); }
@media (max-width: 640px) { .section-card { padding: 16px; } .type-row, .record-row { align-items: stretch; flex-direction: column; gap: 6px; } .form-grid { grid-template-columns: 1fr; } .type-row > :last-child { width: 100%; } .summary-grid { grid-template-columns: 1fr 1fr; gap: 16px; } }
</style>
