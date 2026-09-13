<script setup lang="ts">
/* 游戏库：账号游戏库 / 库分析 / 家庭库 三页签。
   - 账号游戏库 + 库分析 → ownedLib store（GET /owned-library，
     wishlist_items.owned 全量矩阵：绑定账号 + 追踪账户 + 家庭组成员）；
   - 家庭库 → familyLib store（GET /family/library），自 family 页迁入，
     页签激活时由 FamilyLib 自行拉取（familyLib store 的 ready 守卫复用缓存）。 */
import { computed, onMounted, ref, type Component } from 'vue'

import { useI18n, type MessageKey } from '@/locales'
import { HlPaneSwitch, HlTabs, type HlTabItem } from '@/components/ui'
import { useOwnedLibStore } from '@/stores/ownedLib'
import GlOwned from './tabs/GlOwned.vue'
import GlInsights from './tabs/GlInsights.vue'
import FamilyLib from './tabs/FamilyLib.vue'

const { t } = useI18n()
const ownedStore = useOwnedLibStore()
onMounted(() => {
  void ownedStore.load()
})

const activeTab = ref('owned')

/* 页签常量**只存词条 key**：模块级常量存译文会把语言冻在模块加载那一刻，
   渲染期 t(tab.labelKey) 才是响应式的。 */
const TAB_KEYS: { key: string; labelKey: MessageKey }[] = [
  { key: 'owned', labelKey: 'gamelib.tab.owned' },
  { key: 'insights', labelKey: 'gamelib.tab.insights' },
  { key: 'family', labelKey: 'gamelib.tab.family' },
]

const tabItems = computed<HlTabItem[]>(() =>
  TAB_KEYS.map((x) => ({ key: x.key, label: t(x.labelKey) })),
)

/* 页签 → 面板组件。**映射而不是 v-if 链**：面板切换要过渡，<Transition> 只接受
   单个子节点，v-if 链会编译成 fragment，过渡不触发（family 页同款教训）。 */
const PANES: Record<string, Component> = {
  owned: GlOwned,
  insights: GlInsights,
  family: FamilyLib,
}
</script>

<template>
  <section class="gamelib-page">
    <HlTabs v-model="activeTab" :tabs="tabItems" class="gl-tabs">
      <HlPaneSwitch :pane-key="activeTab">
        <component :is="PANES[activeTab]" />
      </HlPaneSwitch>
    </HlTabs>
  </section>
</template>

<style scoped>
.gamelib-page {
  max-width: 1200px;
  margin: 0 auto;
}
.gl-tabs {
  margin-top: 4px;
}
</style>
