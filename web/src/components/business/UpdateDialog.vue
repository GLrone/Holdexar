<script setup lang="ts">
/**
 * 全局更新报告窗口（模糊幕布）——更新模块的唯一交互面。
 *
 * 更新按**应用级**事务处理，不与某个页签绑定：检查 / 了解新版本 / 下载 / 校验 /
 * 重启全在这一个窗口里闭环，幕布（backdrop-filter）遮住底层页面把注意力收拢。
 *
 * 阶段（单一来源 = updater store，本组件不做二次判据）：
 *   checking → latest | unavailable | available → downloading → ready
 *                                    ↘ failed（asset_missing / network / verify_failed）
 *
 * 版式：头部固定（图标 + 按阶段取标题 + 发布日期），中部内容区滚动，底部操作区固定。
 * 下载中「进度块固定在内容区顶部、更新内容在其下独立限高滚动」——进度条不会被日志
 * 顶走，日志也不会被进度条挤没。
 */
import { computed, ref, watch } from 'vue'

import HlButton from '@/components/ui/HlButton.vue'
import HlCheckbox from '@/components/ui/HlCheckbox.vue'
import HlIcon from '@/components/ui/HlIcon.vue'
import { message } from '@/components/ui'
import { systemApi } from '@/api/client'
import { useI18n } from '@/locales'
import { useSettingsStore } from '@/stores/settings'
import { useUpdaterStore } from '@/stores/updater'

import UpdateNotes from './UpdateNotes.vue'

const updater = useUpdaterStore()
const settings = useSettingsStore()
const { t } = useI18n()

const open = computed(() => updater.dialogOpen)
const info = computed(() => updater.info)
const progress = computed(() => updater.progress)

/** 发布页地址（仓库标识由后端下发，前端不硬编码）：手动下载的出路 */
const releasesUrl = ref('')

async function ensureReleasesUrl(): Promise<void> {
  if (releasesUrl.value) return
  try {
    const data = await systemApi.info()
    releasesUrl.value = data.repo ? `https://github.com/${data.repo}/releases` : ''
  } catch {
    /* 拉不到就不给手动下载入口（窗口其余功能不受影响） */
  }
}

watch(open, (visible) => {
  if (visible) void ensureReleasesUrl()
})

type Phase =
  | 'checking'
  | 'latest'
  | 'unavailable'
  | 'available'
  | 'downloading'
  | 'ready'
  | 'failed'

const phase = computed<Phase>(() => {
  if (updater.pendingTag) return 'ready'
  if (updater.downloading) return 'downloading'
  if (updater.failure) return 'failed'
  if (updater.checking) return 'checking'
  if (info.value?.available) return 'available'
  // 检查没成功时不能报「已是最新」：available=false 也可能是网络不可达
  if (info.value?.reason) return 'unavailable'
  if (info.value) return 'latest'
  return 'checking'
})

const latestVersion = computed(() => (info.value?.latest || '').replace(/^v/, ''))
const pendingVersion = computed(() => updater.pendingTag.replace(/^v/, ''))
/** 头部展示的版本：待重启用暂存 tag，其余用清单里的最新版 */
const showVersion = computed(() =>
  phase.value === 'ready' ? pendingVersion.value : latestVersion.value,
)

/** 发布日期：ISO 日期段（清单时间是 UTC，截取即发布当天的稳定口径） */
const releaseDate = computed(() => (info.value?.publishedAt || '').slice(0, 10))

const headTitle = computed(() => {
  const version = showVersion.value
  switch (phase.value) {
    case 'available':
      return t('updateDialog.headAvailable', { version })
    case 'downloading':
      return t('updateDialog.headDownloading', { version: showVersion.value || latestVersion.value })
    case 'ready':
      return t('updateDialog.headReady', { version })
    case 'unavailable':
      return t('updateDialog.headUnavailable')
    case 'failed':
      return t('updateDialog.headFailed')
    case 'latest':
      return t('updateDialog.headLatest')
    default:
      return t('updateDialog.headChecking')
  }
})

const percent = computed(() => Math.max(0, Math.min(100, progress.value?.percent ?? 0)))

/** 已下载/总大小（对齐桌面更新器的口径：有分母看大小，没分母退回百分比） */
const sizeLabel = computed(() => {
  const p = progress.value
  if (!p) return ''
  if (p.total) return `${fmtSize(p.received)} / ${fmtSize(p.total)}`
  return p.percent != null ? `${p.percent}%` : fmtSize(p.received)
})

/** 进度阶段文案：探测/校验/解包各有专属说明，避免看起来卡死 */
const progressText = computed(() => {
  const p = progress.value
  if (!p) return ''
  if (p.phase === 'probe') return t('updateDialog.probe')
  if (p.phase === 'verify') return t('updateDialog.verify')
  if (p.phase === 'extract') return t('updateDialog.extract')
  return sizeLabel.value
})

/** 速率 + 剩余时间 */
const speedText = computed(() => {
  const p = progress.value
  if (!p?.speed) return ''
  const rate = `${fmtSize(p.speed)}/s`
  if (p.total && p.received && p.speed > 0) {
    const eta = Math.max(1, Math.round((p.total - p.received) / p.speed))
    return `${rate} · ${t('updateDialog.eta', { seconds: eta })}`
  }
  return rate
})

/** 失败归因 → 出路文案：不同 code 给不同的「你该怎么办」 */
const failureHint = computed(() => {
  const code = updater.failure?.code
  if (code === 'asset_missing') return t('updateDialog.failAssetMissing')
  if (code === 'network') return t('updateDialog.failNetwork')
  if (code === 'verify_failed') return t('updateDialog.failVerify')
  if (code === 'cancelled') return t('updateDialog.failCancelled')
  return t('updateDialog.failGeneric')
})

const notes = computed(() => info.value?.notes || '')

function fmtSize(bytes: number): string {
  if (!bytes) return '0 B'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`
}

async function onDownload(): Promise<void> {
  const res = await updater.download()
  if (!res.ok && res.error && res.error !== 'no_tag') {
    message.error(t('updateDialog.toastDownloadFailed', { error: res.error }))
  }
}

async function onCancel(): Promise<void> {
  await updater.cancel()
}

async function onRetryCheck(): Promise<void> {
  await updater.check(true)
}

async function onAutoDownloadChange(on: boolean): Promise<void> {
  await settings.setUpdateAuto(on)
}

async function onRestart(): Promise<void> {
  const res = await updater.restartForUpdate()
  if (res.ok) return
  if (res.unsupported) message.info(t('updateDialog.toastRestartUnsupported'))
  else message.error(res.error || t('updateDialog.toastRestartFailed'))
}

function openReleases(): void {
  if (releasesUrl.value) window.open(releasesUrl.value, '_blank')
}

function onClose(): void {
  // 下载中不允许关窗：进度会失去可见落点，用户容易以为卡死了
  if (phase.value === 'downloading') {
    message.info(t('updateDialog.toastCloseBlocked'))
    return
  }
  updater.closeDialog()
}
</script>

<template>
  <Teleport to="body">
    <Transition name="upd-fade" appear>
      <div v-if="open" class="upd-overlay" @click.self="onClose">
        <div class="upd-modal" role="dialog" aria-modal="true" :aria-label="t('updateDialog.title')">
          <header class="upd-head">
            <span class="upd-head__icon"><HlIcon name="download" :size="20" /></span>
            <div class="upd-head__text">
              <h2 class="upd-head__title">{{ headTitle }}</h2>
              <p v-if="releaseDate" class="upd-head__date">
                <HlIcon name="calendar" :size="12" />
                <span>{{ t('updateDialog.releaseDate', { date: releaseDate }) }}</span>
              </p>
            </div>
            <button
              type="button"
              class="upd-head__close"
              :aria-label="t('common.cancel')"
              @click="onClose"
            >
              ✕
            </button>
          </header>

          <div class="upd-body">
            <!-- 检查中 -->
            <p v-if="phase === 'checking'" class="upd-line">
              {{ t('updateDialog.checking') }}
            </p>

            <!-- 已是最新 -->
            <p v-else-if="phase === 'latest'" class="upd-line">
              {{ t('updateDialog.upToDate', { version: info?.current || '' }) }}
            </p>

            <!-- 检查未成功：说清是「查不到」而不是「已最新」 -->
            <div v-else-if="phase === 'unavailable'" class="upd-fail">
              <span class="upd-fail__icon"><HlIcon name="warning" /></span>
              <div>
                <p class="upd-fail__hint">{{ t('updateDialog.checkFailedHint') }}</p>
                <p v-if="info?.error" class="upd-fail__detail">{{ info.error }}</p>
              </div>
            </div>

            <template v-else>
              <!-- 版本跃迁说明（下载中/已就绪阶段不再重复这句） -->
              <p v-if="phase === 'available'" class="upd-line">
                {{
                  t('updateDialog.message', {
                    current: info?.current || '',
                    latest: latestVersion,
                  })
                }}
              </p>

              <!-- 下载中：进度块固定在顶部 -->
              <div v-if="phase === 'downloading'" class="upd-progress">
                <div class="upd-progress__head">
                  <span class="upd-progress__label">{{ t('updateDialog.progressLabel') }}</span>
                  <span class="upd-progress__size hl-num">{{ progressText }}</span>
                </div>
                <div class="upd-progress__bar">
                  <div class="upd-progress__fill" :style="{ width: percent + '%' }" />
                </div>
                <div class="upd-progress__meta">
                  <span v-if="speedText" class="hl-num">{{ speedText }}</span>
                  <span v-if="progress?.channel">{{ t('updateDialog.channel', { channel: progress.channel }) }}</span>
                </div>
              </div>

              <!-- 失败：原因 + 出路 -->
              <div v-else-if="phase === 'failed'" class="upd-fail">
                <span class="upd-fail__icon"><HlIcon name="warning" /></span>
                <div>
                  <p class="upd-fail__hint">{{ failureHint }}</p>
                  <p v-if="updater.failure?.message" class="upd-fail__detail">
                    {{ updater.failure.message }}
                  </p>
                </div>
              </div>

              <!-- 已就绪：等重启 -->
              <div v-else-if="phase === 'ready'" class="upd-ready">
                <span class="upd-ready__icon"><HlIcon name="check-circle" /></span>
                <div>
                  <p class="upd-ready__text">
                    {{ t('updateDialog.readyBody', { version: pendingVersion }) }}
                  </p>
                  <p class="upd-ready__hint">{{ t('updateDialog.readyHint') }}</p>
                </div>
              </div>

              <p v-if="phase === 'available' && info?.sizeBytes" class="upd-size">
                {{ t('updateDialog.packageSize', { size: fmtSize(info.sizeBytes) }) }}
              </p>

              <!-- 自动下载开关（与设置页同一个后端开关） -->
              <HlCheckbox
                v-if="phase === 'available'"
                class="upd-auto"
                :model-value="settings.updateAuto === true"
                :label="t('updateDialog.autoDownload')"
                @update:model-value="onAutoDownloadChange"
              />

              <!-- 更新内容：下载中也在（进度条之下独立限高滚动） -->
              <section v-if="notes" class="upd-notes" :class="{ 'upd-notes--live': phase === 'downloading' }">
                <h3 class="upd-notes__title">{{ t('updateDialog.whatsNew') }}</h3>
                <div class="upd-notes__body">
                  <UpdateNotes :notes="notes" :version="latestVersion" />
                </div>
              </section>
            </template>
          </div>

          <footer class="upd-foot">
            <template v-if="phase === 'downloading'">
              <HlButton variant="text" @click="onCancel">{{ t('updateDialog.cancel') }}</HlButton>
              <HlButton art="combo" tone="blue" disabled>
                {{ t('updateDialog.downloading') }}
              </HlButton>
            </template>

            <template v-else-if="phase === 'ready'">
              <HlButton variant="text" @click="updater.closeDialog()">
                {{ t('updateDialog.later') }}
              </HlButton>
              <HlButton art="combo" tone="green" @click="onRestart">
                <HlIcon name="check" />
                {{ t('updateDialog.restart') }}
              </HlButton>
            </template>

            <template v-else-if="phase === 'failed'">
              <HlButton variant="text" @click="updater.closeDialog()">
                {{ t('updateDialog.later') }}
              </HlButton>
              <HlButton v-if="releasesUrl" variant="default" @click="openReleases">
                {{ t('updateDialog.manualDownload') }}
              </HlButton>
              <HlButton art="combo" tone="blue" @click="onDownload">
                <HlIcon name="refresh" />
                {{ t('updateDialog.retry') }}
              </HlButton>
            </template>

            <template v-else-if="phase === 'available'">
              <HlButton variant="text" @click="updater.skipVersion(latestVersion)">
                {{ t('updateDialog.skip') }}
              </HlButton>
              <HlButton v-if="releasesUrl" variant="default" @click="openReleases">
                {{ t('updateDialog.manualDownload') }}
              </HlButton>
              <HlButton art="combo" tone="green" @click="onDownload">
                <HlIcon name="download" />
                {{ t('updateDialog.updateNow') }}
              </HlButton>
            </template>

            <template v-else-if="phase === 'unavailable'">
              <HlButton variant="text" @click="updater.closeDialog()">
                {{ t('updateDialog.later') }}
              </HlButton>
              <HlButton v-if="releasesUrl" variant="default" @click="openReleases">
                {{ t('updateDialog.manualDownload') }}
              </HlButton>
              <HlButton art="combo" tone="blue" @click="onRetryCheck">
                <HlIcon name="refresh" />
                {{ t('updateDialog.retry') }}
              </HlButton>
            </template>

            <template v-else>
              <HlButton variant="text" @click="updater.closeDialog()">
                {{ t('common.cancel') }}
              </HlButton>
            </template>
          </footer>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
/* 幕布：半透明底 + 背景模糊。模糊是「注意力收拢」的手段——底层页面仍在，
   但视觉上退到后面，用户不会再去点它（参考用法见 .hl-overlay 一族）。 */
.upd-overlay {
  position: fixed;
  inset: 0;
  z-index: 10000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
  background: rgba(15, 23, 42, 0.55);
  backdrop-filter: blur(10px);
  -webkit-backdrop-filter: blur(10px);
  overflow-y: auto;
}

.upd-modal {
  width: 100%;
  max-width: 620px;
  max-height: 85vh;
  display: flex;
  flex-direction: column;
  background: var(--surface-pop-deep, var(--bg-card));
  border: 1px solid var(--border-soft);
  border-radius: 16px;
  box-shadow: var(--shadow-xl);
  overflow: hidden;
}

.upd-head {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 18px 20px 14px;
  border-bottom: 1px solid var(--border-soft);
}

.upd-head__icon {
  flex-shrink: 0;
  width: 40px;
  height: 40px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 12px;
  background: var(--accent-soft);
  color: var(--accent);
}

.upd-head__text {
  flex: 1;
  min-width: 0;
}

.upd-head__title {
  margin: 0;
  font-size: 17px;
  font-weight: 700;
  color: var(--text-primary);
}

.upd-head__date {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin: 4px 0 0;
  font-size: 11.5px;
  color: var(--text-muted);
}

.upd-head__close {
  flex-shrink: 0;
  width: 30px;
  height: 30px;
  border: none;
  border-radius: 8px;
  background: transparent;
  color: var(--text-muted);
  font-size: 14px;
  cursor: pointer;
  transition:
    color calc(var(--duration-1) * var(--motion-scale)) var(--ease-inout),
    background calc(var(--duration-1) * var(--motion-scale)) var(--ease-inout);
}

.upd-head__close:hover {
  color: var(--text-primary);
  background: var(--accent-a10);
}

.upd-body {
  flex: 1;
  min-height: 0;
  padding: 16px 20px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.upd-line {
  margin: 0;
  font-size: 13.5px;
  line-height: 1.7;
  color: var(--text-secondary);
}

.upd-progress {
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.upd-progress__head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
}

.upd-progress__label {
  font-size: 13.5px;
  font-weight: 600;
  color: var(--text-primary);
}

.upd-progress__size {
  font-size: 12.5px;
  color: var(--text-primary);
}

.upd-progress__bar {
  height: 8px;
  border-radius: 4px;
  background: var(--accent-a15);
  overflow: hidden;
}

.upd-progress__fill {
  height: 100%;
  border-radius: 4px;
  background: var(--accent-fill);
  transition: width calc(var(--duration-2) * var(--motion-scale)) var(--ease-inout);
}

.upd-progress__meta {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  font-size: 11.5px;
  color: var(--text-muted);
}

.upd-fail,
.upd-ready {
  display: flex;
  gap: 10px;
  padding: 11px 13px;
  border-radius: 10px;
  font-size: 12.5px;
  line-height: 1.65;
}

.upd-fail {
  background: var(--danger-a15);
  color: var(--danger);
}

.upd-ready {
  background: var(--success-a15);
  color: var(--success);
}

.upd-fail__icon,
.upd-ready__icon {
  flex-shrink: 0;
  margin-top: 1px;
}

.upd-fail__hint,
.upd-ready__text {
  margin: 0;
}

.upd-fail__detail {
  margin: 5px 0 0;
  font-size: 11px;
  opacity: 0.75;
  word-break: break-all;
}

.upd-ready__hint {
  margin: 4px 0 0;
  font-size: 11.5px;
  opacity: 0.8;
}

.upd-size {
  margin: 0;
  font-size: 11.5px;
  color: var(--text-muted);
}

.upd-auto {
  font-size: 12.5px;
}

.upd-notes {
  border-top: 1px solid var(--border-soft);
  padding-top: 12px;
}

.upd-notes__title {
  margin: 0 0 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

.upd-notes__body {
  max-height: 190px;
  overflow-y: auto;
  padding-right: 6px;
}

/* 下载中给更新内容更高的上限：进度块已经在最上面，这里可以多露几行 */
.upd-notes--live .upd-notes__body {
  max-height: 240px;
}

.upd-foot {
  display: flex;
  justify-content: flex-end;
  gap: 9px;
  padding: 13px 20px 16px;
  border-top: 1px solid var(--border-soft);
}

/* 入场：位移 + 透明度（红线要求：不用 scale，且必须挂 --motion-scale 总闸） */
.upd-fade-enter-active,
.upd-fade-leave-active {
  transition: opacity calc(var(--duration-2) * var(--motion-scale)) var(--ease-inout);
}

.upd-fade-enter-from,
.upd-fade-leave-to {
  opacity: 0;
}

.upd-fade-enter-active .upd-modal,
.upd-fade-leave-active .upd-modal {
  transition: transform calc(var(--duration-2) * var(--motion-scale)) var(--ease-inout);
}

.upd-fade-enter-from .upd-modal,
.upd-fade-leave-to .upd-modal {
  transform: translateY(10px);
}
</style>