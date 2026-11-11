<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { HlButton, HlCheckbox, HlDialog, HlEmpty, HlInput, HlPaneSwitch, HlSelect, HlSkeleton } from '@/components/ui'
import { useI18n } from '@/locales'
import { pluginsApi, pluginsChangedEvent, type PluginField, type PluginsState, type PluginView } from '@/lib/pluginsApi'

const { t } = useI18n()
const state = ref<PluginsState>({ builtin: [], installed: [], catalog: [] })
const tab = ref<'installed' | 'builtin' | 'discover'>('installed')
const query = ref('')
const source = ref('')
const loading = ref(true)
const busy = ref(false)
const error = ref('')
const feedback = ref('')
const catalogError = ref('')
const selected = ref<PluginView | null>(null)
const detailsOpen = ref(false)
const removeOpen = ref(false)
const localOpen = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)
const localFile = ref<File | null>(null)
const draft = ref<Record<string, string | number | boolean>>({})
const installed = computed(() => selected.value && state.value.installed.find(p => p.manifest.id === selected.value?.manifest.id))
const fields = computed(() => Object.entries(selected.value?.manifest.configSchema?.properties ?? {}))
const visible = computed(() => {
  const list = tab.value === 'discover' ? state.value.catalog : state.value.installed
  const q = query.value.trim().toLocaleLowerCase()
  return list.filter(p => `${p.manifest.name} ${p.manifest.id} ${p.manifest.description}`.toLocaleLowerCase().includes(q))
})

function messageOf(problem: unknown) {
  return problem instanceof Error ? problem.message : t('plugins.failed')
}

async function reload() {
  state.value = await pluginsApi.list()
  catalogError.value = state.value.catalogError ?? ''
  if (selected.value) {
    selected.value = state.value.installed.find(p => p.manifest.id === selected.value?.manifest.id) ?? selected.value
  }
}

async function retryLoad() {
  loading.value = true
  error.value = ''
  try { await reload() } catch (problem) { error.value = messageOf(problem) }
  finally { loading.value = false }
}

async function run(action: () => Promise<unknown>, success = '') {
  if (busy.value) return
  busy.value = true
  error.value = ''
  feedback.value = ''
  try {
    await action()
    await reload()
    window.dispatchEvent(new Event(pluginsChangedEvent))
    feedback.value = success || t('plugins.done')
  } catch (problem) {
    error.value = messageOf(problem)
  } finally {
    busy.value = false
  }
}

function openDetails(plugin: PluginView) {
  selected.value = plugin
  draft.value = {}
  const saved = state.value.installed.find(item => item.manifest.id === plugin.manifest.id)
  for (const [key, field] of Object.entries(plugin.manifest.configSchema?.properties ?? {})) {
    draft.value[key] = field.format === 'password' ? '' : saved?.config?.[key] ?? plugin.config?.[key] ?? (field.type === 'boolean' ? false : '')
  }
  detailsOpen.value = true
}

function setField(key: string, field: PluginField, value: string | number | Array<string | number>) {
  const scalar = Array.isArray(value) ? value[0] ?? '' : value
  draft.value[key] = field.enum?.find(option => String(option) === String(scalar)) ?? scalar
}

async function saveConfig() {
  if (!selected.value) return
  const config: Record<string, unknown> = {}
  const required = selected.value.manifest.configSchema?.required ?? []
  for (const [key, field] of fields.value) {
    const value = draft.value[key]
    if (field.format === 'password' && value === '' && (installed.value?.configured || installed.value?.configuredSecrets?.includes(key))) continue
    if (required.includes(key) && (value === '' || value === undefined)) {
      error.value = t('plugins.required', { field: field.title || key })
      return
    }
    if (field.type === 'number' && value === '' && !required.includes(key)) continue
    if (field.type === 'number' && (value === '' || !Number.isFinite(Number(value)))) {
      error.value = t('plugins.number', { field: field.title || key })
      return
    }
    config[key] = field.type === 'number' ? Number(value) : value
  }
  await run(() => pluginsApi.update(selected.value!.manifest.id, { config }))
  if (!error.value) {
    for (const [key, field] of fields.value) if (field.format === 'password') draft.value[key] = ''
  }
}

function homepage(plugin: PluginView) {
  try {
    const url = new URL(plugin.manifest.homepage ?? '')
    return url.protocol === 'https:' && !url.username && !url.password ? url.href : ''
  } catch { return '' }
}

async function discover() {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    const result = await pluginsApi.discover(source.value.trim(), query.value)
    state.value.catalog = result.items
    catalogError.value = result.error ? t('plugins.offline', { reason: result.error }) : ''
  } catch (problem) {
    error.value = messageOf(problem)
  } finally {
    busy.value = false
  }
}

function chooseFile(event: Event) {
  localFile.value = (event.target as HTMLInputElement).files?.[0] ?? null
  localOpen.value = localFile.value !== null
  if (fileInput.value) fileInput.value.value = ''
}

async function installSelected() {
  if (!selected.value) return
  await run(() => pluginsApi.install(selected.value!))
  if (!error.value) { detailsOpen.value = false; tab.value = 'installed' }
}

async function installLocal() {
  if (!localFile.value) return
  await run(() => pluginsApi.installLocal(localFile.value!))
  if (!error.value) { localOpen.value = false; localFile.value = null; tab.value = 'installed' }
}

async function uninstall() {
  if (!selected.value) return
  await run(() => pluginsApi.remove(selected.value!.manifest.id))
  if (!error.value) { removeOpen.value = false; detailsOpen.value = false }
}

onMounted(async () => {
  await retryLoad()
})
</script>

<template>
  <section class="card plugins-settings" data-section="plugins.title" :aria-busy="busy || loading">
    <div class="section-title">{{ t('plugins.title') }}</div>
    <p class="section-desc">{{ t('plugins.description') }}</p>
    <div class="plugins-settings__tools" role="group" :aria-label="t('plugins.title')">
      <HlButton v-for="item in (['installed', 'builtin', 'discover'] as const)" :key="item" size="sm" :variant="tab === item ? 'primary' : 'default'" :aria-pressed="tab === item" @click="tab = item">{{ t(`plugins.${item}`) }}</HlButton>
      <HlButton size="sm" :disabled="busy || loading" @click="retryLoad">{{ t('plugins.reload') }}</HlButton>
    </div>
    <p v-if="error" class="plugins-settings__error" role="alert">{{ error }}</p>
    <p v-if="feedback" class="section-desc" role="status">{{ feedback }}</p>
    <p v-if="busy" class="section-desc" role="status">{{ t('plugins.working') }}</p>
    <HlSkeleton v-if="loading" variant="text" :rows="3" :count="1" />
    <HlPaneSwitch v-else :pane-key="tab">
      <div v-if="tab === 'builtin'" class="plugins-settings__list">
        <div v-for="item in state.builtin" :key="item.id" class="plugins-settings__item">
          <div><strong>{{ item.name || item.id }}</strong><p class="section-desc">{{ item.id === 'core.epic_free' ? t('plugins.epicHint') : t('plugins.builtinHint') }}</p><span class="section-desc">{{ item.running ? t('plugins.running') : t('plugins.stopped') }}</span></div>
          <HlButton size="sm" :disabled="busy" @click="run(() => pluginsApi.builtin(item.id, !item.enabled))">{{ item.enabled ? t('plugins.disable') : t('plugins.enable') }}</HlButton>
        </div>
      </div>
      <div v-else>
        <div class="plugins-settings__tools">
          <label class="plugins-settings__search"><span>{{ t('plugins.search') }}</span><HlInput v-model="query" :placeholder="t('plugins.search')" /></label>
          <HlButton size="sm" :disabled="busy" @click="fileInput?.click()">{{ t('plugins.local') }}</HlButton>
          <input ref="fileInput" type="file" accept=".hlx" hidden :aria-label="t('plugins.local')" @change="chooseFile" />
        </div>
        <div v-if="tab === 'discover'" class="plugins-settings__tools">
          <label class="plugins-settings__search"><span>{{ t('plugins.source') }}</span><HlInput v-model="source" :placeholder="t('plugins.source')" /></label>
          <HlButton size="sm" :disabled="busy" @click="discover">{{ t('plugins.refresh') }}</HlButton>
        </div>
        <p v-if="tab === 'discover' && catalogError" class="section-desc" role="status">{{ catalogError }}</p>
        <p class="section-desc">{{ t('plugins.single') }}</p>
        <div class="plugins-settings__list">
          <article v-for="plugin in visible" :key="plugin.manifest.id" class="plugins-settings__item">
            <div class="plugins-settings__summary"><strong>{{ plugin.manifest.name }}</strong><span class="plugins-settings__version">{{ plugin.manifest.version }}</span><p>{{ plugin.manifest.description }}</p><span class="section-desc">{{ !plugin.compatible ? t('plugins.incompatible') : plugin.enabled ? t('plugins.enabled') : t('plugins.disabled') }}</span><p v-if="plugin.error" class="plugins-settings__error">{{ plugin.error }}</p></div>
            <HlButton size="sm" @click="openDetails(plugin)">{{ t('plugins.details') }}</HlButton>
          </article>
          <HlEmpty v-if="!visible.length" size="sm" :text="t('plugins.empty')" />
        </div>
      </div>
    </HlPaneSwitch>
    <HlDialog v-model="detailsOpen" :title="selected?.manifest.name" :width="620">
      <div v-if="selected" class="plugins-settings__details">
        <p>{{ selected.manifest.description }}</p>
        <dl><dt>{{ t('plugins.author') }}</dt><dd>{{ selected.manifest.author }}</dd><dt>{{ t('plugins.version') }}</dt><dd>{{ selected.manifest.version }}</dd></dl>
        <a v-if="homepage(selected)" :href="homepage(selected)" target="_blank" rel="noopener noreferrer">{{ t('plugins.homepage') }}</a>
        <p>{{ t('plugins.kind') }}</p>
        <p>{{ t('plugins.permissions') }}</p><ul><li v-for="domain in selected.manifest.domains" :key="domain">{{ domain }}</li></ul>
        <p class="plugins-settings__warning">{{ t('plugins.trust') }}</p>
        <p class="section-desc">{{ selected.trust === 'verified' ? t('plugins.verified') : t('plugins.unsigned') }}</p>
        <p v-if="!selected.compatible" class="plugins-settings__error">{{ t('plugins.incompatible') }}</p>
        <p v-if="selected.error" class="plugins-settings__error">{{ selected.error }}</p>
        <template v-if="installed">
          <p v-if="!installed.configured" class="section-desc">{{ t('plugins.configure') }}</p>
          <label v-for="[key, field] in fields" :key="key" class="plugins-settings__field">
            <span>{{ field.title || key }}{{ selected.manifest.configSchema?.required?.includes(key) ? ' *' : '' }}</span>
            <HlSelect v-if="field.enum" :model-value="String(draft[key] ?? '')" :options="field.enum.map(value => ({ value: String(value), label: typeof value === 'boolean' ? t(value ? 'plugins.true' : 'plugins.false') : String(value) }))" @update:model-value="value => setField(key, field, value)" />
            <HlCheckbox v-else-if="field.type === 'boolean'" :model-value="draft[key] === true" @update:model-value="value => draft[key] = value" />
            <HlInput v-else :model-value="String(draft[key] ?? '')" :type="field.type === 'number' ? 'number' : 'text'" :show-password="field.format === 'password'" :placeholder="field.format === 'password' && installed.configured ? t('plugins.secret') : ''" @update:model-value="value => draft[key] = value" />
          </label>
          <div class="plugins-settings__tools"><HlButton :disabled="busy" @click="saveConfig">{{ t('plugins.save') }}</HlButton><HlButton :disabled="busy || !installed.configured" @click="run(() => pluginsApi.test(selected!.manifest.id), t('plugins.testSent'))">{{ t('plugins.test') }}</HlButton></div>
          <p class="section-desc">{{ t('plugins.removeHint') }}</p>
          <div class="plugins-settings__tools"><HlButton :disabled="busy || !selected.compatible" @click="run(() => pluginsApi.update(selected!.manifest.id, { enabled: !installed!.enabled }))">{{ installed.enabled ? t('plugins.disable') : t('plugins.enable') }}</HlButton><HlButton v-if="selected.manifest.version !== installed.manifest.version" :disabled="busy || !selected.compatible" @click="installSelected">{{ t('plugins.update') }}</HlButton><HlButton variant="danger" :disabled="busy" @click="removeOpen = true">{{ t('plugins.remove') }}</HlButton></div>
        </template>
        <HlButton v-else variant="primary" :loading="busy" :disabled="!selected.compatible" @click="installSelected">{{ t('plugins.install') }}</HlButton>
        <p v-if="error" class="plugins-settings__error" role="alert">{{ error }}</p><p v-if="feedback" role="status">{{ feedback }}</p><p v-if="busy" role="status">{{ t('plugins.working') }}</p>
      </div>
    </HlDialog>
    <HlDialog v-model="removeOpen" :title="t('plugins.removeConfirm')"><p>{{ t('plugins.removeHint') }}</p><template #footer><HlButton :disabled="busy" @click="removeOpen = false">{{ t('plugins.cancel') }}</HlButton><HlButton variant="danger" :loading="busy" @click="uninstall">{{ t('plugins.removeConfirm') }}</HlButton></template></HlDialog>
    <HlDialog v-model="localOpen" :title="t('plugins.localConfirm')"><p>{{ localFile?.name }}</p><p>{{ t('plugins.trust') }}</p><p v-if="error" class="plugins-settings__error" role="alert">{{ error }}</p><template #footer><HlButton :disabled="busy" @click="localOpen = false">{{ t('plugins.cancel') }}</HlButton><HlButton variant="primary" :loading="busy" @click="installLocal">{{ t('plugins.install') }}</HlButton></template></HlDialog>
  </section>
</template>

<style scoped>
.plugins-settings { padding: 24px; min-width: 0; }
.plugins-settings__tools { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin: 14px 0; }
.plugins-settings__tools > .hl-input-wrap { flex: 1; min-width: 180px; }
.plugins-settings__search { display: grid; flex: 1; gap: 6px; min-width: 180px; }
.plugins-settings__list { display: grid; gap: 12px; }
.plugins-settings__item { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; border-top: 1px solid var(--line-1); padding-top: 16px; }
.plugins-settings__summary { min-width: 0; overflow-wrap: anywhere; }
.plugins-settings__summary p { margin: 8px 0; }
.plugins-settings__version { margin-left: 10px; color: var(--text-muted); }
.plugins-settings__error { color: var(--danger); overflow-wrap: anywhere; }
.plugins-settings__warning { color: var(--warning); }
.plugins-settings__details { display: grid; gap: 12px; overflow-wrap: anywhere; }
.plugins-settings__details p, .plugins-settings__details ul { margin: 0; }
.plugins-settings__details dl { display: grid; grid-template-columns: auto 1fr; gap: 8px 16px; margin: 0; }
.plugins-settings__details dd { margin: 0; }
.plugins-settings__field { display: grid; gap: 8px; }
.plugins-settings__field :deep(.hl-select-wrap) { width: 100%; min-width: 0; }
</style>
