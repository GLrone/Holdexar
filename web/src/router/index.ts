import { createRouter, createWebHistory } from 'vue-router'

import type { MessageKey } from '@/locales'

/**
 * 路由页标题走**键**而非字面量（`meta.titleKey` → App.vue 的 pageTitle → document.title）。
 *
 * 包一层函数而不是直接写 `meta: { titleKey: 'nav.dashboard' }`：对象字面量里的
 * 字符串会被推断成 `string`，拼错 key 不会报错，只会让浏览器标签页空白一片——
 * 一个没人会立刻发现的 bug。这里的形参类型把它拉回 MessageKey 联合，
 * 拼错即编译期失败。
 */
const titleKey = (key: MessageKey) => ({ titleKey: key })

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/dashboard' },
    {
      path: '/dashboard',
      component: () => import('@/views/dashboard/Index.vue'),
      meta: titleKey('nav.dashboard'),
    },
    {
      path: '/library',
      component: () => import('@/views/library/Index.vue'),
      meta: titleKey('nav.library'),
    },
    {
      path: '/gamelib',
      component: () => import('@/views/gamelib/Index.vue'),
      meta: titleKey('nav.gamelib'),
    },
    {
      path: '/achievements',
      component: () => import('@/views/achievements/Index.vue'),
      meta: titleKey('nav.achievements'),
    },
    {
      path: '/game/:appid',
      component: () => import('@/views/game-detail/Index.vue'),
      meta: titleKey('nav.gameDetail'),
    },
    {
      path: '/pool',
      component: () => import('@/views/pool/Index.vue'),
      meta: titleKey('nav.pool'),
    },
    // 旧愿望单页 URL（书签/外链）重定向到监控池，避免掉进 404 兜底
    { path: '/wishlist', redirect: '/pool' },
    {
      path: '/bundles',
      component: () => import('@/views/bundles/Index.vue'),
      meta: titleKey('nav.bundles'),
    },
    {
      path: '/crawl',
      component: () => import('@/views/crawl/Index.vue'),
      meta: titleKey('nav.crawl'),
    },
    {
      path: '/proxies',
      component: () => import('@/views/proxies/Index.vue'),
      meta: titleKey('nav.proxies'),
    },
    {
      path: '/alerts',
      component: () => import('@/views/alerts/Index.vue'),
      meta: titleKey('nav.alerts'),
    },
    {
      path: '/rates',
      component: () => import('@/views/rates/Index.vue'),
      meta: titleKey('nav.rates'),
    },
    {
      path: '/logs',
      component: () => import('@/views/logs/Index.vue'),
      meta: titleKey('nav.logs'),
    },
    {
      path: '/toolbox',
      component: () => import('@/views/toolbox/Index.vue'),
      meta: titleKey('nav.toolbox'),
    },
    {
      path: '/family',
      component: () => import('@/views/family/Index.vue'),
      meta: titleKey('nav.family'),
    },
    {
      path: '/bills',
      component: () => import('@/views/bills/Index.vue'),
      meta: titleKey('nav.bills'),
    },
    {
      path: '/settings',
      component: () => import('@/views/settings/Index.vue'),
      meta: titleKey('nav.me'),
    },
    {
      path: '/about',
      component: () => import('@/views/about/Index.vue'),
      meta: titleKey('nav.about'),
    },
    { path: '/:pathMatch(.*)*', redirect: '/dashboard' },
  ],
})

export default router
