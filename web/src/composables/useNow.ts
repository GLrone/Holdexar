import { onBeforeUnmount, onMounted, ref } from 'vue'

/**
 * 共享的「现在」时刻 ref（默认 1s 步进）。
 *
 * 倒计时类组件的数据源：组件挂载才起定时器、卸载即清，多个组件各自持有
 * 互不干扰。后台标签页的定时器会被浏览器节流，回前台时值会一次跳到位——
 * 倒计时显示的是时刻差而非流逝脉冲，节流不影响正确性。
 */
export function useNow(intervalMs = 1000) {
  const now = ref(Date.now())
  let timer: number | undefined

  onMounted(() => {
    timer = window.setInterval(() => {
      now.value = Date.now()
    }, intervalMs)
  })

  onBeforeUnmount(() => {
    if (timer !== undefined) window.clearInterval(timer)
  })

  return now
}
