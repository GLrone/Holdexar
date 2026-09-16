import { defineStore } from 'pinia'

import type { SortKey, FilterMode } from '@/api/regions'

/**
 * 筛选状态 store。
 * ownership / 系列字段暂未接入数据源，保留结构位。
 *
 * 生命周期：本 store 只在**游戏商店页驻留期间**有效——离开页面时
 * `resetForLeave()` 把搜索词与全部筛选清回默认（见 views/library 的
 * onBeforeUnmount），下次进入是干净默认态，不会带回上一轮搜索结果。
 * 布局模式与高级筛选面板开合属纯 UI 偏好，不在重置范围。
 */
interface FilterState {
  search: string
  committedSearch: string
  sortBy: SortKey
  region: string
  filterMode: FilterMode
  layoutMode: 'grid' | 'list'
  onlyDiscounted: boolean
  isLowest: boolean
  onlyHb: boolean
  onlyEpic: boolean
  onlyXgp: boolean
  hideOwned: boolean
  /** 屏蔽家庭共享（非主账户已拥有=家人库可玩；与卡片「家庭共享」徽章同口径） */
  hideFamilySharing: boolean
  minPrice: string
  maxPrice: string
  minRating: string
  maxRating: string
  minReviews: string
  maxReviews: string
  diffMin: string
  diffMax: string
  diffType: 'absolute' | 'percent'
  strictLowest: boolean
  tolerance: string
  top3Check: boolean
  hlNew: boolean
  hlEqual: boolean
  hlNon: boolean
  giftFilter: boolean
  /** 游戏商店默认隐藏 DLC（白名单豁免个别常驻 DLC） */
  excludeDlc: boolean
  filterRegion: string
  showAdvancedFilter: boolean

  setFilter: (key: string, value: unknown) => void
  commitSearch: () => void
  resetFilters: () => void
  resetForLeave: () => void
  activeFilterCount: () => number
}

export const useFilterStore = defineStore('gamesFilter', {
  state: (): FilterState => ({
    search: '',
    committedSearch: '',
    // 默认排序 = 智能推荐（新算法）；旧算法仍可从排序下拉手动选择
    sortBy: 'smart',
    region: '',
    filterMode: 'global',
    layoutMode: 'grid',
    onlyDiscounted: false,
    isLowest: false,
    onlyHb: false,
    onlyEpic: false,
    onlyXgp: false,
    hideOwned: false,
    hideFamilySharing: false,
    minPrice: '',
    maxPrice: '',
    minRating: '',
    maxRating: '',
    minReviews: '',
    maxReviews: '',
    diffMin: '',
    diffMax: '',
    diffType: 'absolute',
    strictLowest: false,
    tolerance: '0',
    top3Check: true,
    hlNew: false,
    hlEqual: false,
    hlNon: false,
    giftFilter: false,
    excludeDlc: true,
    filterRegion: 'cn',
    showAdvancedFilter: false,

    setFilter(key: string, value: unknown) {
      ;(this as unknown as Record<string, unknown>)[key] = value
    },
    commitSearch() {
      this.committedSearch = this.search
    },
    resetFilters() {
      this.sortBy = 'smart'
      this.region = ''
      this.filterMode = 'global'
      this.onlyDiscounted = false
      this.isLowest = false
      this.onlyHb = false
      this.onlyEpic = false
      this.onlyXgp = false
      this.hideOwned = false
      this.hideFamilySharing = false
      this.minPrice = ''
      this.maxPrice = ''
      this.minRating = ''
      this.maxRating = ''
      this.minReviews = ''
      this.maxReviews = ''
      this.diffMin = ''
      this.diffMax = ''
      this.diffType = 'absolute'
      this.strictLowest = false
      this.tolerance = '0'
      this.top3Check = true
      this.hlNew = false
      this.hlEqual = false
      this.hlNon = false
      this.giftFilter = false
      this.excludeDlc = true
      this.filterRegion = 'cn'
    },

    /** 离开游戏商店页时调用：搜索词 + 全部筛选 + 面板开合回默认。
     *  「搜索结果只活在当前页面」——从侧边栏去别的页再回来，看到的是默认态，
     *  而不是上一轮搜过的结果。布局模式（网格/列表）是纯外观偏好，保留。 */
    resetForLeave() {
      this.search = ''
      this.committedSearch = ''
      this.resetFilters()
      this.showAdvancedFilter = false
    },

    activeFilterCount() {
      let count = 0
      if (this.onlyDiscounted) count++
      if (this.isLowest) count++
      if (this.onlyHb) count++
      if (this.onlyEpic) count++
      if (this.onlyXgp) count++
      if (this.hideOwned) count++
      if (this.hideFamilySharing) count++
      if (this.strictLowest) count++
      if (!this.top3Check) count++
      if (this.hlNew) count++
      if (this.hlEqual) count++
      if (this.hlNon) count++
      if (this.giftFilter) count++
      if (!this.excludeDlc) count++
      if (this.minPrice) count++
      if (this.maxPrice) count++
      if (this.minRating) count++
      if (this.maxRating) count++
      if (this.minReviews) count++
      if (this.maxReviews) count++
      if (this.diffMin) count++
      if (this.diffMax) count++
      if (this.diffType === 'percent') count++
      if (this.filterRegion !== 'cn') count++
      if (this.region) count++
      if (this.filterMode !== 'global') count++
      return count
    },
  }),
})
