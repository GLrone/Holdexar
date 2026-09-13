import { defineStore } from 'pinia'

import type { SortKey, FilterMode } from '@/api/regions'

/**
 * 筛选状态 store。
 * ownership / 系列字段暂未接入数据源，保留结构位。
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
  filterRegion: string
  showAdvancedFilter: boolean

  setFilter: (key: string, value: unknown) => void
  commitSearch: () => void
  resetFilters: () => void
  activeFilterCount: () => number
}

export const useFilterStore = defineStore('gamesFilter', {
  state: (): FilterState => ({
    search: '',
    committedSearch: '',
    sortBy: 'default',
    region: '',
    filterMode: 'global',
    layoutMode: 'grid',
    onlyDiscounted: false,
    isLowest: false,
    onlyHb: false,
    onlyEpic: false,
    onlyXgp: false,
    hideOwned: false,
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
    filterRegion: 'cn',
    showAdvancedFilter: false,

    setFilter(key: string, value: unknown) {
      ;(this as unknown as Record<string, unknown>)[key] = value
    },
    commitSearch() {
      this.committedSearch = this.search
    },
    resetFilters() {
      this.sortBy = 'default'
      this.region = ''
      this.filterMode = 'global'
      this.onlyDiscounted = false
      this.isLowest = false
      this.onlyHb = false
      this.onlyEpic = false
      this.onlyXgp = false
      this.hideOwned = false
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
      this.filterRegion = 'cn'
    },

    activeFilterCount() {
      let count = 0
      if (this.onlyDiscounted) count++
      if (this.isLowest) count++
      if (this.onlyHb) count++
      if (this.onlyEpic) count++
      if (this.onlyXgp) count++
      if (this.hideOwned) count++
      if (this.strictLowest) count++
      if (!this.top3Check) count++
      if (this.hlNew) count++
      if (this.hlEqual) count++
      if (this.hlNon) count++
      if (this.giftFilter) count++
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
