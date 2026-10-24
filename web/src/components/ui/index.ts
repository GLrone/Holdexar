/* ════════════════════════════════════════════════════════════════════
   Holdexar UI 框架层 —— 统一出口（L2 束口）
   视图层唯一入口：import { HlButton, message } from '@/components/ui'
   或 main.ts 中 app.use(hlUi) 后全局直接使用 <HlButton> 等。
   规则：
   · 视图禁止直接书写 el-* 组件 / element-plus 导入；
   · 新颜色只加 tokens.css 浅深两套；组件层只引令牌；
   · 新组件先在 component-framework.html 双主题过目，再封装进本目录。
   ════════════════════════════════════════════════════════════════════ */
import type { App } from 'vue'

import HlAvatar from './HlAvatar.vue'
import HlBanner from './HlBanner.vue'
import HlButton from './HlButton.vue'
import HlCarousel from './HlCarousel.vue'
import HlCheckbox from './HlCheckbox.vue'
import HlDatePicker from './HlDatePicker.vue'
import HlDialog from './HlDialog.vue'
import HlChip from './HlChip.vue'
import HlDrawer from './HlDrawer.vue'
import HlDropdown from './HlDropdown.vue'
import HlEmpty from './HlEmpty.vue'
import HlIcon from './HlIcon.vue'
import HlImg from './HlImg.vue'
import HlInput from './HlInput.vue'
import HlIsland from './HlIsland.vue'
import HlLangToggle from './HlLangToggle.vue'
import HlPagination from './HlPagination.vue'
import HlPaneSwitch from './HlPaneSwitch.vue'
import HlPopconfirm from './HlPopconfirm.vue'
import HlProgress from './HlProgress.vue'
import HlScrollList from './HlScrollList.vue'
import HlSegmented from './HlSegmented.vue'
import HlSelect from './HlSelect.vue'
import HlSectionRail from './HlSectionRail.vue'
import HlSideNav from './HlSideNav.vue'
import HlSkeleton from './HlSkeleton.vue'
import HlSpinner from './HlSpinner.vue'
import HlStat from './HlStat.vue'
import HlStepper from './HlStepper.vue'
import HlSwitch from './HlSwitch.vue'
import HlTable from './HlTable.vue'
import HlTabs from './HlTabs.vue'
import HlTag from './HlTag.vue'
import HlTextarea from './HlTextarea.vue'
import HlThemeToggle from './HlThemeToggle.vue'
import HlUndoToast from './HlUndoToast.vue'
import HlTooltip from './HlTooltip.vue'
import HlTopbarAvatar from './HlTopbarAvatar.vue'

export { ICONS, isIconName, type IconName } from './icons'
export { message, type MessageType } from './message'
export {
  activityManager,
  ActivityManager,
  islandController,
  IslandController,
  type Activity,
  type ActivityStatus,
  type ActivityKind,
  type ActivityTone,
  type ActivityRegistration,
} from './activity'
export type { HlSelectOption } from './HlSelect.vue'
export type { HlTableColumn } from './HlTable.vue'
export type { HlTabItem } from './HlTabs.vue'
export type { HlSideNavGroup } from './HlSideNav.vue'

export {
  HlAvatar,
  HlBanner,
  HlButton,
  HlCarousel,
  HlCheckbox,
  HlDatePicker,
  HlDialog,
  HlDrawer,
  HlDropdown,
  HlChip,
  HlEmpty,
  HlIcon,
  HlImg,
  HlInput,
  HlIsland,
  HlLangToggle,
  HlPagination,
  HlPaneSwitch,
  HlPopconfirm,
  HlProgress,
  HlScrollList,
  HlSegmented,
  HlSelect,
  HlSectionRail,
  HlSideNav,
  HlSkeleton,
  HlSpinner,
  HlStat,
  HlStepper,
  HlSwitch,
  HlTable,
  HlTabs,
  HlTag,
  HlTextarea,
  HlThemeToggle,
  HlUndoToast,
  HlTooltip,
  HlTopbarAvatar,
}

/** 全局注册（Hl 前缀，模板中直接 <HlButton> 使用） */
export default function install(app: App) {
  app.component('HlAvatar', HlAvatar)
  app.component('HlBanner', HlBanner)
  app.component('HlButton', HlButton)
  app.component('HlCarousel', HlCarousel)
  app.component('HlCheckbox', HlCheckbox)
  app.component('HlDatePicker', HlDatePicker)
  app.component('HlDialog', HlDialog)
  app.component('HlDrawer', HlDrawer)
  app.component('HlDropdown', HlDropdown)
  app.component('HlEmpty', HlEmpty)
  app.component('HlIcon', HlIcon)
  app.component('HlImg', HlImg)
  app.component('HlInput', HlInput)
  app.component('HlIsland', HlIsland)
  app.component('HlLangToggle', HlLangToggle)
  app.component('HlPagination', HlPagination)
  app.component('HlChip', HlChip)
  app.component('HlPaneSwitch', HlPaneSwitch)
  app.component('HlPopconfirm', HlPopconfirm)
  app.component('HlProgress', HlProgress)
  app.component('HlScrollList', HlScrollList)
  app.component('HlSegmented', HlSegmented)
  app.component('HlSelect', HlSelect)
  app.component('HlSectionRail', HlSectionRail)
  app.component('HlSideNav', HlSideNav)
  app.component('HlSkeleton', HlSkeleton)
  app.component('HlSpinner', HlSpinner)
  app.component('HlStat', HlStat)
  app.component('HlStepper', HlStepper)
  app.component('HlSwitch', HlSwitch)
  app.component('HlTable', HlTable)
  app.component('HlTabs', HlTabs)
  app.component('HlTag', HlTag)
  app.component('HlTextarea', HlTextarea)
  app.component('HlThemeToggle', HlThemeToggle)
  app.component('HlUndoToast', HlUndoToast)
  app.component('HlTooltip', HlTooltip)
  app.component('HlTopbarAvatar', HlTopbarAvatar)
}
