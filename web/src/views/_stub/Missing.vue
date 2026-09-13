<script setup lang="ts">
/**
 * 「施工中」占位视图 —— vite 插件 stub-missing-views 的顶替目标。
 *
 * 路由可能先于视图文件落地（构建期占位防线）；
 * 此组件让构建与运行不至于整体失败。正式视图写入原路径后自动生效，
 * 本文件永远不该被路由直接引用（check:routes 会拦截悬空引用入库）。
 */
import { useRoute } from 'vue-router'
import { useI18n } from '@/locales'

const route = useRoute()
const { t } = useI18n()
</script>

<template>
  <div class="view-stub">
    <div class="view-stub__icon">🚧</div>
    <div class="view-stub__title">{{ t('stub.title') }}</div>
    <div class="view-stub__path">{{ route.path }}</div>
    <div class="view-stub__hint">{{ t('stub.hint') }}</div>
  </div>
</template>

<style scoped>
.view-stub {
  min-height: 60vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: var(--text-muted);
}

.view-stub__icon {
  font-size: 40px;
}

.view-stub__title {
  font-size: 16px;
  font-weight: 600;
  color: var(--text-primary);
}

.view-stub__path {
  font-family: var(--font-mono, monospace);
  font-size: 12px;
  color: var(--accent);
  background: var(--accent-soft, rgba(28, 110, 164, 0.08));
  padding: 2px 10px;
  border-radius: 999px;
}

.view-stub__hint {
  max-width: 420px;
  text-align: center;
  font-size: 12px;
  line-height: 1.7;
}
</style>
