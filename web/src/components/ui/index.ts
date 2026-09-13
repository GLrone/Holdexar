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

import HlAlert from './HlAlert.vue'
import HlAvatar from './HlAvatar.vue'
import HlBacktop from './HlBacktop.vue'
import HlBadge from './HlBadge.vue'
import HlBanner from './HlBanner.vue'
import HlBreadcrumb from './HlBreadcrumb.vue'
import HlButton from './HlButton.vue'
import HlCarousel from './HlCarousel.vue'
import HlCheckbox from './HlCheckbox.vue'
import HlCollapse from './HlCollapse.vue'
import HlDatePicker from './HlDatePicker.vue'
import HlDescriptions from './HlDescriptions.vue'
import HlDialog from './HlDialog.vue'
import HlChip from './HlChip.vue'
import HlDrawer from './HlDrawer.vue'
import HlDropdown from './HlDropdown.vue'
import HlEmpty from './HlEmpty.vue'
import HlFormItem from './HlFormItem.vue'
import HlGooeyNav from './HlGooeyNav.vue'
import HlIcon from './HlIcon.vue'
import HlImg from './HlImg.vue'
import HlInput from './HlInput.vue'
import HlInputNumber from './HlInputNumber.vue'
import HlLangToggle from './HlLangToggle.vue'
import HlPagination from './HlPagination.vue'
import HlPaneSwitch from './HlPaneSwitch.vue'
import HlPopconfirm from './HlPopconfirm.vue'
import HlProgress from './HlProgress.vue'
import HlRadioGroup from './HlRadioGroup.vue'
import HlRate from './HlRate.vue'
import HlScrollList from './HlScrollList.vue'
import HlSegmented from './HlSegmented.vue'
import HlSelect from './HlSelect.vue'
import HlSectionRail from './HlSectionRail.vue'
import HlSideNav from './HlSideNav.vue'
import HlSkeleton from './HlSkeleton.vue'
import HlSlider from './HlSlider.vue'
import HlSpinner from './HlSpinner.vue'
import HlStat from './HlStat.vue'
import HlStepper from './HlStepper.vue'
import HlSteps from './HlSteps.vue'
import HlSwitch from './HlSwitch.vue'
import HlTable from './HlTable.vue'
import HlTabs from './HlTabs.vue'
import HlTag from './HlTag.vue'
import HlTextarea from './HlTextarea.vue'
import HlThemeToggle from './HlThemeToggle.vue'
import HlTimeline from './HlTimeline.vue'
import HlTooltip from './HlTooltip.vue'
import HlTopbarAvatar from './HlTopbarAvatar.vue'
import HlTree from './HlTree.vue'

export { ICONS, isIconName, type IconName } from './icons'
export { message, type MessageType } from './message'
export type { HlSelectOption } from './HlSelect.vue'
export type { HlTableColumn } from './HlTable.vue'
export type { HlTabItem } from './HlTabs.vue'
export type { HlBreadcrumbItem } from './HlBreadcrumb.vue'
export type { HlCollapseItem } from './HlCollapse.vue'
export type { HlDropdownItem } from './HlDropdown.vue'
export type { HlDescriptionItem } from './HlDescriptions.vue'
export type { HlTimelineItem } from './HlTimeline.vue'
export type { HlTreeNode } from './HlTree.vue'
export type { HlStepperStep } from './HlStepper.vue'
export type { HlBannerItem } from './HlBanner.vue'
export type { HlGooeyNavItem } from './HlGooeyNav.vue'
export type { HlSideNavItem, HlSideNavGroup } from './HlSideNav.vue'
export type { HlSectionRailItem } from './HlSectionRail.vue'

export {
  HlAlert,
  HlAvatar,
  HlBacktop,
  HlBadge,
  HlBanner,
  HlBreadcrumb,
  HlButton,
  HlCarousel,
  HlCheckbox,
  HlCollapse,
  HlDatePicker,
  HlDescriptions,
  HlDialog,
  HlDrawer,
  HlDropdown,
  HlChip,
  HlEmpty,
  HlFormItem,
  HlGooeyNav,
  HlIcon,
  HlImg,
  HlInput,
  HlInputNumber,
  HlLangToggle,
  HlPagination,
  HlPaneSwitch,
  HlPopconfirm,
  HlProgress,
  HlRadioGroup,
  HlRate,
  HlScrollList,
  HlSegmented,
  HlSelect,
  HlSectionRail,
  HlSideNav,
  HlSkeleton,
  HlSlider,
  HlSpinner,
  HlStat,
  HlStepper,
  HlSteps,
  HlSwitch,
  HlTable,
  HlTabs,
  HlTag,
  HlTextarea,
  HlThemeToggle,
  HlTimeline,
  HlTooltip,
  HlTopbarAvatar,
  HlTree,
}

/** 全局注册（Hl 前缀，模板中直接 <HlButton> 使用） */
export default function install(app: App) {
  app.component('HlAlert', HlAlert)
  app.component('HlAvatar', HlAvatar)
  app.component('HlBacktop', HlBacktop)
  app.component('HlBadge', HlBadge)
  app.component('HlBanner', HlBanner)
  app.component('HlBreadcrumb', HlBreadcrumb)
  app.component('HlButton', HlButton)
  app.component('HlCarousel', HlCarousel)
  app.component('HlCheckbox', HlCheckbox)
  app.component('HlCollapse', HlCollapse)
  app.component('HlDatePicker', HlDatePicker)
  app.component('HlDescriptions', HlDescriptions)
  app.component('HlDialog', HlDialog)
  app.component('HlDrawer', HlDrawer)
  app.component('HlDropdown', HlDropdown)
  app.component('HlEmpty', HlEmpty)
  app.component('HlFormItem', HlFormItem)
  app.component('HlGooeyNav', HlGooeyNav)
  app.component('HlIcon', HlIcon)
  app.component('HlImg', HlImg)
  app.component('HlInput', HlInput)
  app.component('HlInputNumber', HlInputNumber)
  app.component('HlLangToggle', HlLangToggle)
  app.component('HlPagination', HlPagination)
  app.component('HlChip', HlChip)
  app.component('HlPaneSwitch', HlPaneSwitch)
  app.component('HlPopconfirm', HlPopconfirm)
  app.component('HlProgress', HlProgress)
  app.component('HlRadioGroup', HlRadioGroup)
  app.component('HlRate', HlRate)
  app.component('HlScrollList', HlScrollList)
  app.component('HlSegmented', HlSegmented)
  app.component('HlSelect', HlSelect)
  app.component('HlSectionRail', HlSectionRail)
  app.component('HlSideNav', HlSideNav)
  app.component('HlSkeleton', HlSkeleton)
  app.component('HlSlider', HlSlider)
  app.component('HlSpinner', HlSpinner)
  app.component('HlStat', HlStat)
  app.component('HlStepper', HlStepper)
  app.component('HlSteps', HlSteps)
  app.component('HlSwitch', HlSwitch)
  app.component('HlTable', HlTable)
  app.component('HlTabs', HlTabs)
  app.component('HlTag', HlTag)
  app.component('HlTextarea', HlTextarea)
  app.component('HlThemeToggle', HlThemeToggle)
  app.component('HlTimeline', HlTimeline)
  app.component('HlTooltip', HlTooltip)
  app.component('HlTopbarAvatar', HlTopbarAvatar)
  app.component('HlTree', HlTree)
}
