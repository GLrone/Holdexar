import type { Directive } from 'vue'

/**
 * v-stagger —— 逐项级联入场（.hl-stagger）的序号分配器。
 *
 * 容器挂 .hl-stagger（动画与时长约定见 styles/hl-framework.css），本指令只负责
 * 给子项写 --hl-stagger-i 序号：只给还没有序号的子项分配——首屏 0..n；后续追加
 * 的每批重新从 0 起算（各自级联）；已在 DOM 的老卡不动，动画不重播。
 *
 * 分配不走组件 updated 钩子：子树无变化的纯重渲染（如切语言）对容器不保证触发
 * 该钩子；MutationObserver 盯真实子项增删，才与 v-for 追加/过滤严格同步。回调在
 * 微任务检查点执行，仍早于首次绘制，延迟期由 backwards 填充兜住不闪首帧。
 */
const observers = new WeakMap<HTMLElement, MutationObserver>()

function assignIndexes(el: HTMLElement): void {
  let batch = 0
  for (const child of Array.from(el.children)) {
    const node = child as HTMLElement
    if (node.style.getPropertyValue('--hl-stagger-i') !== '') continue
    node.style.setProperty('--hl-stagger-i', String(batch++))
  }
}

export const vStagger: Directive<HTMLElement> = {
  mounted(el) {
    assignIndexes(el)
    const mo = new MutationObserver(() => assignIndexes(el))
    mo.observe(el, { childList: true })
    observers.set(el, mo)
  },
  unmounted(el) {
    observers.get(el)?.disconnect()
    observers.delete(el)
  },
}
