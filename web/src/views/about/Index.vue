<script setup lang="ts">
/* 关于：项目级说明页（侧边栏「系统」分组）。
   章节范式对齐 GitHub 开源项目 README（定位 / 功能 / 技术栈 / 数据来源 / 隐私 / 致谢）；
   运行环境实时读 /system/info。内容对齐 app_info.py 与各模块真实实现，勿写愿景。 */
import { computed, onMounted, ref } from 'vue'

import { systemApi } from '@/api/client'
import { APP_NAME, APP_SUBTITLE_KEY } from '@/appInfo'
import { useI18n, type MessageKey } from '@/locales'
import { useThemeStore } from '@/stores/theme'
import { HlIcon } from '@/components/ui'

const { t } = useI18n()

const themeStore = useThemeStore()
const logo = computed(() =>
  themeStore.isDark ? '/assets/logo_dark.ico' : '/assets/logo_light.ico',
)

interface SystemInfo {
  app: string
  version: string
  python: string
  platform: string
  data_dir: string
  web_dist_ready: boolean
  uptime_seconds: number
}
const info = ref<SystemInfo | null>(null)

onMounted(async () => {
  try {
    info.value = await systemApi.info()
  } catch {
    /* 后端未就绪时静态内容照常展示，仅运行环境卡缺省 */
  }
})

const version = computed(() => info.value?.version ?? '')
/** 运行时长：分档文案走词典。t() 在 computed 里现取，切语言即重算——
 *  若把结果写进 ref（如模块级常量）会把语言冻在求值那一刻。 */
const uptime = computed(() => {
  const s = Math.max(0, Math.floor(info.value?.uptime_seconds ?? 0))
  if (s < 60) return t('about.uptime.seconds', { n: s })
  const m = Math.floor(s / 60)
  if (m < 60) return t('about.uptime.minutes', { n: m })
  const h = Math.floor(m / 60)
  if (h < 24) return t('about.uptime.hours', { h, m: m % 60 })
  return t('about.uptime.days', { d: Math.floor(h / 24), h: h % 24 })
})

/* 功能特性（与侧边栏模块一一对应，勿夸大）。
   ⚠️ 模块级常量只在模块加载时求值一次——**存 key 不存文案**，显示文本在模板里
   t() 现取，否则英文界面下这一整块永远是中文。 */
const FEATURES: { icon: string; titleKey: MessageKey; descKey: MessageKey }[] = [
  { icon: 'chart', titleKey: 'about.feature.multiRegion.title', descKey: 'about.feature.multiRegion.desc' },
  { icon: 'star', titleKey: 'about.feature.wishlist.title', descKey: 'about.feature.wishlist.desc' },
  { icon: 'bell', titleKey: 'about.feature.alerts.title', descKey: 'about.feature.alerts.desc' },
  { icon: 'arrow-left-right', titleKey: 'about.feature.rates.title', descKey: 'about.feature.rates.desc' },
  { icon: 'package', titleKey: 'about.feature.bundles.title', descKey: 'about.feature.bundles.desc' },
  { icon: 'home', titleKey: 'about.feature.family.title', descKey: 'about.feature.family.desc' },
  { icon: 'list', titleKey: 'about.feature.bills.title', descKey: 'about.feature.bills.desc' },
  { icon: 'globe', titleKey: 'about.feature.proxies.title', descKey: 'about.feature.proxies.desc' },
  { icon: 'refresh', titleKey: 'about.feature.tasks.title', descKey: 'about.feature.tasks.desc' },
  { icon: 'zap', titleKey: 'about.feature.toolbox.title', descKey: 'about.feature.toolbox.desc' },
]

/* 致谢：随包分发的开源内核与素材源。
   项目名与外链是专名（不翻译），只有说明走词典且同样**存 key 不存文案**。 */
const CREDITS: { name: string; url: string; noteKey: MessageKey }[] = [
  {
    name: 'mihomo',
    url: 'https://github.com/MetaCubeX/mihomo',
    noteKey: 'about.credit.mihomo',
  },
]
</script>

<template>
  <section class="about-page">
    <!-- 定位（hero） -->
    <div class="card about-hero">
      <img class="about-hero__logo" :src="logo" alt="" />
      <div class="about-hero__main">
        <div class="about-hero__name">
          <h1>{{ APP_NAME }}</h1>
          <span v-if="version" class="about-ver">v{{ version }}</span>
        </div>
        <div class="about-hero__sub">{{ t(APP_SUBTITLE_KEY) }}</div>
        <p class="about-hero__desc">{{ t('about.hero.desc') }}</p>
      </div>
    </div>

    <!-- 功能特性 -->
    <div class="sec-title">{{ t('about.section.features') }}</div>
    <div class="about-feats">
      <div v-for="f in FEATURES" :key="f.titleKey" class="card about-feat">
        <div class="about-feat__icon"><HlIcon :name="f.icon" :size="17" /></div>
        <div>
          <div class="about-feat__title">{{ t(f.titleKey) }}</div>
          <div class="about-feat__desc">{{ t(f.descKey) }}</div>
        </div>
      </div>
    </div>

    <div class="about-cols">
      <!-- 技术栈 -->
      <div>
        <div class="sec-title">{{ t('about.section.tech') }}</div>
        <div class="card about-plain">
          <ul class="about-list">
            <li><b>{{ t('about.tech.frontend.label') }}</b> — {{ t('about.tech.frontend.desc') }}</li>
            <li><b>{{ t('about.tech.backend.label') }}</b> — {{ t('about.tech.backend.desc') }}</li>
            <li><b>{{ t('about.tech.desktop.label') }}</b> — {{ t('about.tech.desktop.desc') }}</li>
            <li><b>{{ t('about.tech.proxy.label') }}</b> — {{ t('about.tech.proxy.desc') }}</li>
          </ul>
        </div>
      </div>

      <!-- 数据来源 -->
      <div>
        <div class="sec-title">{{ t('about.section.sources') }}</div>
        <div class="card about-plain">
          <ul class="about-list">
            <li><b>{{ t('about.source.price.label') }}</b> — {{ t('about.source.price.desc') }}</li>
            <li><b>{{ t('about.source.account.label') }}</b> — {{ t('about.source.account.desc') }}</li>
            <li><b>{{ t('about.source.network.label') }}</b> — {{ t('about.source.network.desc') }}</li>
            <li>
              <b>{{ t('about.source.flags.label') }}</b> — {{ t('about.source.flags.desc') }}
              <b>{{ t('about.source.rates.label') }}</b> — {{ t('about.source.rates.desc') }}
            </li>
          </ul>
        </div>
      </div>
    </div>

    <!-- 隐私与免责 -->
    <div class="sec-title">{{ t('about.section.privacy') }}</div>
    <div class="card about-plain">
      <ul class="about-list">
        <li>{{ t('about.privacy.localOnly') }}</li>
        <li>{{ t('about.privacy.noBypass') }}</li>
        <li>{{ t('about.privacy.disclaimer') }}</li>
        <li>{{ t('about.privacy.personalUse') }}</li>
      </ul>
    </div>

    <!-- 致谢 -->
    <div class="sec-title">{{ t('about.section.credits') }}</div>
    <div class="card about-plain">
      <ul class="about-list">
        <li v-for="c in CREDITS" :key="c.name">
          <a :href="c.url" target="_blank" rel="noopener noreferrer">{{ c.name }}</a>
          <span class="about-credit-note">—— {{ t(c.noteKey) }}</span>
        </li>
      </ul>
    </div>

    <!-- 运行环境（/system/info 实时） -->
    <div class="card about-env">
      <template v-if="info">
        <span class="about-env__item"><HlIcon name="info" :size="13" /> {{ t('about.env.version', { v: info.version }) }}</span>
        <span class="about-env__item">Python {{ info.python }}</span>
        <span class="about-env__item mono">{{ info.platform }}</span>
        <span class="about-env__item mono" :title="info.data_dir">{{ t('about.env.dataDir', { path: info.data_dir }) }}</span>
        <span class="about-env__item">{{ t('about.env.uptime', { d: uptime }) }}</span>
      </template>
      <span v-else class="about-env__item">{{ t('about.env.disconnected') }}</span>
    </div>
  </section>
</template>

<style scoped>
.about-page {
  max-width: 1080px;
  margin: 0 auto;
  display: grid;
  gap: 16px;
}

/* ── hero ── */
.about-hero { display: flex; align-items: center; gap: 20px; padding: 26px 28px; }
.about-hero__logo { width: 64px; height: 64px; flex-shrink: 0; }
.about-hero__name { display: flex; align-items: center; gap: 10px; }
.about-hero__name h1 { margin: 0; font-size: 24px; font-weight: 800; color: var(--text-primary); letter-spacing: 0.5px; }
.about-ver {
  font-size: 11.5px;
  font-family: var(--font-mono, monospace);
  padding: 2px 9px;
  border-radius: 999px;
  background: var(--accent-a15);
  color: var(--accent);
}
.about-hero__sub { font-size: 13px; color: var(--text-muted); margin-top: 3px; }
.about-hero__desc { font-size: 13px; color: var(--text-secondary); line-height: 1.9; margin: 12px 0 0; max-width: 720px; }
.about-hero__desc b, .about-hero__desc strong { color: var(--text-primary); }

/* ── 章节标题（fx-sub 同款强调条） ── */
.sec-title {
  font-size: 13px;
  font-weight: 700;
  color: var(--text-primary);
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 6px;
}
.sec-title::before { content: ''; width: 3px; height: 13px; border-radius: 2px; background: var(--accent-fill); }

/* ── 功能特性 ── */
.about-feats { display: grid; grid-template-columns: repeat(auto-fill, minmax(310px, 1fr)); gap: 12px; }
.about-feat { display: flex; gap: 12px; padding: 15px 17px; }
.about-feat__icon {
  width: 34px; height: 34px; flex-shrink: 0;
  border-radius: var(--radius, 8px);
  display: grid; place-items: center;
  background: var(--accent-a15);
  color: var(--accent);
}
.about-feat__title { font-size: 13.5px; font-weight: 700; color: var(--text-primary); }
.about-feat__desc { font-size: 12px; color: var(--text-muted); line-height: 1.75; margin-top: 4px; }

/* ── 双栏区 ── */
.about-cols { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 900px) { .about-cols { grid-template-columns: 1fr; } }

/* ── 列表型卡片 ── */
.about-plain { padding: 18px 22px; }
.about-list { list-style: none; padding: 0; margin: 0; display: grid; gap: 9px; }
.about-list li {
  position: relative;
  padding-left: 15px;
  font-size: 12.5px;
  color: var(--text-secondary);
  line-height: 1.8;
}
.about-list li::before {
  content: '';
  position: absolute;
  left: 0;
  top: 0.72em;
  width: 5px;
  height: 5px;
  border-radius: 999px;
  background: var(--accent-fill);
}
.about-list b { color: var(--text-primary); font-weight: 600; }
.about-list a { color: var(--accent); text-decoration: none; border-bottom: 1px dashed var(--accent-fill); }
.about-list a:hover { border-bottom-style: solid; }
.about-credit-note { color: var(--text-muted); }

/* ── 运行环境页脚 ── */
.about-env {
  display: flex; flex-wrap: wrap; gap: 8px 18px;
  padding: 13px 20px;
  font-size: 11.5px;
  color: var(--text-dim);
}
.about-env__item { display: inline-flex; align-items: center; gap: 6px; }
.mono { font-family: var(--font-mono, monospace); }
</style>
