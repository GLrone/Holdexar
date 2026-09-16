<script setup lang="ts">
/**
 * 全局更新弹窗（模糊幕布）——更新模块的唯一交互面。
 *
 * 为什么从设置页搬出来：更新是**应用级**事务，不该塞在某个页签里。旧形态下
 * 用户点了「检查更新」要在设置页里找下载按钮，下载中切走再回来进度还得靠
 * store 续上；现在检查 / 下载 / 进度 / 校验 / 重启全在这一个弹窗里闭环，
 * 幕布（backdrop-filter）遮住底层页面，用户的注意力与操作都被收在这里。
 *
 * 状态机（单一来源 = updater store，本组件不做二次判据）：
 *   checking → available → downloading → ready（暂存就绪，等重启）
 *                       ↘ failed（asset_missing / network / verify_failed）
 */
import { computed, ref, watch } from 'vue'

import HlButton from '@/components/ui/HlButton.vue'
import HlIcon from '@/components/ui/HlIcon.vue'
import { message } from '@/components/ui'
import { systemApi } from '@/api/client'
import { useI18n } from '@/locales'
import { useUpdaterStore } from '@/stores/updater'

const updater = useUpdaterStore()
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
    /* 拉不到就不给手动下载入口（弹窗其余功能不受影响） */
  }
}

watch(open, (visible) => {
  if (visible) void ensureReleasesUrl()
})

type Phase = 'checking' | 'available' | 'downloading' | 'ready' | 'failed' | 'latest'

const phase = computed<Phase>(() => {
  if (updater.pendingTag) return 'ready'
  if (updater.downloading) return 'downloading'
  if (updater.failure) return 'failed'
  if (updater.checking) return 'checking'
  if (info.value?.available) return 'available'
  if (info.value) return 'latest'
  return 'checking'
})

const latestVersion = computed(() => (info.value?.latest || '').replace(/^v/, ''))
const percent = computed(() => Math.max(0, Math.min(100, progress.value?.percent ?? 0)))

/** 进度文案：有百分比给百分比，没有（通道未给总量）给已下体积，总之不能空白 */
const progressText = computed(() => {
  const p = progress.value
  if (!p) return ''
  if (p.phase === 'probe') return t('updateDialog.probe')
  if (p.phase === 'verify') return t('updateDialog.verify')
  if (p.phase === 'extract') return t('updateDialog.extract')
  if (p.percent != null) return `${p.percent}%`
  return fmtSize(p.received)
})

/** 速率 + 剩余量：下载最怕「看起来卡死」，有这两项就知道它在动 */
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
  // 下载中不允许关窗：关掉就等于把进度藏起来，用户会以为又卡死了
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
            <span class="upd-head__icon"><HlIcon name="download" /></span>
            <div class="upd-head__text">
              <h2 class="upd-head__title">{{ t('updateDialog.title') }}</h2>
              <p v-if="latestVersion" class="upd-head__sub">
                {{ t('updateDialog.version', { version: latestVersion }) }}
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

            <!-- 有新版 / 下载中 / 失败：先说清「从哪个版本升到哪个版本」 -->
            <p v-else class="upd-line">
              {{ t('updateDialog.message', { current: info?.current || '', latest: latestVersion }) }}
            </p>

            <!-- 下载进度 -->
            <div v-if="phase === 'downloading'" class="upd-progress">
              <div class="upd-progress__bar">
                <div class="upd-progress__fill" :style="{ width: percent + '%' }" />
              </div>
              <div class="upd-progress__meta">
                <span>{{ progressText }}</span>
                <span v-if="speedText" class="upd-progress__speed">{{ speedText }}</span>
              </div>
              <p v-if="progress?.channel" class="upd-progress__channel">
                {{ t('updateDialog.channel', { channel: progress.channel }) }}
              </p>
            </div>

            <!-- 失败：说清原因 + 给出路 -->
            <div v-else-if="phase === 'failed'" class="upd-fail">
              <span class="upd-fail__icon"><HlIcon name="info" /></span>
              <div>
                <p class="upd-fail__hint">{{ failureHint }}</p>
                <p v-if="updater.failure?.message" class="upd-fail__detail">
                  {{ updater.failure.message }}
                </p>
              </div>
            </div>

            <!-- 已就绪：等重启 -->
            <div v-else-if="phase === 'ready'" class="upd-ready">
              <span class="upd-ready__icon"><HlIcon name="check" /></span>
              <div>
                <p class="upd-ready__text">
                  {{ t('updateDialog.readyBody', { version: updater.pendingTag.replace(/^v/, '') }) }}
                </p>
                <p class="upd-ready__hint">{{ t('updateDialog.readyHint') }}</p>
              </div>
            </div>

            <!-- 更新说明 -->
            <div v-if="info?.notes && phase !== 'downloading'" class="upd-notes">
              <h3 class="upd-notes__title">{{ t('updateDialog.whatsNew') }}</h3>
              <pre class="upd-notes__body">{{ info.notes }}</pre>
            </div>

            <p v-if="phase === 'available' && info?.sizeBytes" class="upd-size">
              {{ t('updateDialog.packageSize', { size: fmtSize(info.sizeBytes) }) }}
            </p>
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
  width: 34px;
  height: 34px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 10px;
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

.upd-head__sub {
  margin: 2px 0 0;
  font-size: 12.5px;
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
  gap: 6px;
}

.upd-progress__bar {
  height: 6px;
  border-radius: 3px;
  background: var(--accent-a10);
  overflow: hidden;
}

.upd-progress__fill {
  height: 100%;
  border-radius: 3px;
  background: var(--accent-fill);
  transition: width calc(var(--duration-2) * var(--motion-scale)) var(--ease-inout);
}

.upd-progress__meta {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  font-size: 12px;
  color: var(--text-secondary);
}

.upd-progress__speed {
  color: var(--text-muted);
}

.upd-progress__channel {
  margin: 0;
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
  background: rgba(231, 76, 60, 0.1);
  color: var(--danger);
}

.upd-ready {
  background: rgba(164, 208, 7, 0.12);
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
  margin: 0;
  max-height: 190px;
  overflow-y: auto;
  padding-right: 6px;
  font-family: inherit;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--text-secondary);
  white-space: pre-wrap;
  word-break: break-word;
}

.upd-size {
  margin: 0;
  font-size: 11.5px;
  color: var(--text-muted);
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
