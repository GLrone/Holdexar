import { defineStore } from 'pinia'
import { ref } from 'vue'

/** 产品导览开关：全局唯一，浮层只挂 App.vue 一份（跨路由存活）。
 *  页面级入口（关于页 hero logo、设置页「重看教程」）调用 open()，不自己挂
 *  ProductTour——页面实例会在导览第一步 router.push 时随视图卸载，导览消失。
 *  打开时 ProductTour 内部把 step 归 0，重开一律从头走。 */
export const useTourStore = defineStore('tour', () => {
  const open = ref(false)

  function show() {
    open.value = true
  }

  return { open, show }
})
