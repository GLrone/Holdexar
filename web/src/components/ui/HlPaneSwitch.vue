<script setup lang="ts">
/**
 * 板块 / 模块切换过渡。
 *
 * 三条约定（各处自己写 `<Transition>` 时也会踩）：
 *
 * ① **过渡类必须带显式 duration。** Vue 在匹配不到任何 CSS 过渡时，靠 rAF 双帧判定
 *    「无过渡、立即完成」。窗口不可见（后台标签页）时 rAF 不跑，判定永不落地，
 *    `<Transition mode="out-in">` 会把新内容**永久卡在等待里**——整块渲染为空。
 *    `.hl-pane-*` 的时长是显式的（`calc(var(--duration-N) * var(--motion-scale))`），
 *    组件层不传 `:duration` 兜底——两处都写会让实际时长取决于谁更短，难以推理。
 *
 * ② **时长必须挂 `--motion-scale`。** 系统级「减少动态效果」下总闸归零，板块切换
 *    不再位移；直接写 `0.25s` 的组件会绕过这个闸。
 *
 * ③ **方向要统一。** 旧内容向上淡出、新内容自下淡入（`.hl-pane-*` 一处定义）——
 *    各处自己写会出现「这个模块从左进、那个从下进」。
 *
 * 用法：
 * ```vue
 * <HlPaneSwitch :pane-key="activeTab">
 *   <component :is="panes[activeTab]" />
 * </HlPaneSwitch>
 * ```
 * 路由级切换同理，传 `route.path`。
 */
withDefaults(
  defineProps<{
    /** 切换标识：值变化即触发过渡（tab 名 / 索引 / route.path 均可） */
    paneKey: string | number
    /** 过渡名，对应 hl-framework.css 的 `.hl-pane-*` */
    name?: string
    /** 出入场顺序。默认 out-in：旧内容先走、新内容再来，不会两屏叠在一起 */
    mode?: 'out-in' | 'in-out' | 'default'
    /** 首帧是否播放。默认 false——首次挂载不该有「从下往上冒」的入场 */
    appear?: boolean
    /**
     * 包裹元素标签。默认 `div`。
     * `<Transition>` 要求单个根节点，所以这里必然多一层盒子：父容器是 grid / flex
     * 且对该层有强约束时，把它设成与原来一致的标签（如 `section` / `ul`）或
     * 直接传 `template` 让调用方自己保证单根。
     */
    tag?: string
  }>(),
  { name: 'hl-pane', mode: 'out-in', appear: false, tag: 'div' },
)
</script>

<template>
  <Transition :name="name" :mode="mode" :appear="appear">
    <component :is="tag" :key="paneKey">
      <slot />
    </component>
  </Transition>
</template>
