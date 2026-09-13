import { createPinia } from 'pinia'
import { computed, createApp } from 'vue'

import App from './App.vue'
import router from './router'
import { APP_NAME } from './appInfo'

// ── 样式加载顺序有约束：EP 基础样式在前，主题覆盖在后 ──
import ElementPlus, { provideGlobalConfig } from 'element-plus'
import epZhCn from 'element-plus/es/locale/lang/zh-cn'
import epEn from 'element-plus/es/locale/lang/en'
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'

import './styles/tokens.css'
// 框架标准层（hl-* 隔离类名，规范来源 web/public/component-framework.html）
import './styles/hl-framework.css'
// 库页等保真翻译视图的页面样式（globals + gamecard 全量）
import './styles/steamhl-globals.css'
import './styles/steamhl-gamecard.css'
// 捆绑包浏览视图专属规则（复用 game-card 骨架，主题化字面量）
import './styles/hl-bundles.css'
// 浅色适配层必须最后加载（覆盖移植样式的深色表面）
import './styles/steamhl-light.css'

import { useThemeStore } from './stores/theme'
import { useLocaleStore } from './stores/locale'
import { useRegionsStore } from './stores/regions'
// 框架束口层：全局注册 Hl* 组件（视图层唯一入口，见 components/ui/index.ts）
import hlUi from './components/ui'
// 全局快捷键：F5 / Ctrl+R 强制刷新网页（桌面终端没有浏览器刷新键）
import { setupForceReload } from './lib/forceReload'

const app = createApp(App)
const pinia = createPinia()

for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(key, component)
}

app.use(pinia)
app.use(router)
// 语言**不在这里注入**：`app.use(ElementPlus, { locale })` 是启动期一次性求值，
// 切换语言后 el-table 空数据文案、el-select 无选项提示等内置文案不会跟着变
// （全项目 34 处 el-table 都会漏中文）。刻意不传 options，把「写 globalConfig
// 的第一次」让给本文件末尾那次响应式的 provideGlobalConfig——理由见那里的注释。
app.use(ElementPlus)
app.use(hlUi)
setupForceReload()

// 主题：读取 localStorage 持久化偏好（默认深色），挂载前应用
const themeStore = useThemeStore(pinia)
themeStore.apply()
// 界面语言：同款挂载前应用（html.lang + localStorage，词典见 @/locales）
const localeStore = useLocaleStore(pinia)
localeStore.apply()

// ── Element Plus 内置文案随语言切换（表格空数据、下拉无选项、日期面板、
//    MessageBox 按钮…）──
// 三件事决定了它必须落在这里、且必须用 provideGlobalConfig：
// ① 不能用 `app.use(ElementPlus, { locale })`——那是启动期一次性求值，切了不变
//    （全项目 34 处 el-table 都会漏中文）。改用 app.use(ElementPlus) 不带 options：
//    EP 的 installer 是 `if (options) provideGlobalConfig(options, app, true)`，
//    不传 options 就**不会**抢先写死那份模块级 globalConfig，留给我们写。
// ② 命令式的 ElMessageBox / ElMessage 不走组件树 inject，读的正是这份模块级
//    globalConfig，而它只在第一次 provideGlobalConfig 时写入——所以「第一次」必
//    须是我们这一次，且写进去的是**响应式** context，切语言时确认框按钮也变。
// ③ 放在 main.ts 而非 App.vue：视图层有「禁止直连 Element Plus」的红线
//    （组件一律走 components/ui），而这里本就是 EP 的兼容层注册处。
provideGlobalConfig(
  computed(() => ({ locale: localeStore.locale === 'zh-CN' ? epZhCn : epEn })),
  app,
)
document.title = APP_NAME

// 区服元数据：挂载前从服务端拉一次（priceMatrix/下拉/筛选都依赖，本地接口毫秒级）
useRegionsStore(pinia)
  .load()
  .finally(() => {
    app.mount('#app')
  })
