/**
 * Holdexar Structured UI System — 组件统一注册中心。
 *
 * 集中管理系统全部结构化展示组件的元数据、Schema 契约、渲染器绑定。
 */
import type { Component } from 'vue'
import type { ComponentMeta, StructuredBlock } from './types'

// P0 通用核心组件
import MetricGrid from './components/MetricGrid.vue'
import DataTable from './components/DataTable.vue'
import TimeSeries from './components/TimeSeries.vue'
import CoverageMatrix from './components/CoverageMatrix.vue'
import StatusBoard from './components/StatusBoard.vue'

// 既有业务卡种（解耦后的独立组件）
import PriceFactsCard from './legacy/PriceFactsCard.vue'
import GamesFactsCard from './legacy/GamesFactsCard.vue'
import ActionFactsCard from './legacy/ActionFactsCard.vue'
import NavigateFactsCard from './legacy/NavigateFactsCard.vue'
import RowsFactsCard from './legacy/RowsFactsCard.vue'
import FamilyFactsCard from './legacy/FamilyFactsCard.vue'
import AchievementsFactsCard from './legacy/AchievementsFactsCard.vue'
import ProposalFactsCard from './legacy/ProposalFactsCard.vue'

// 既有外置独立卡种
import HlPilotCompare from '@/components/business/HlPilotCompare.vue'
import HlPilotEpic from '@/components/business/HlPilotEpic.vue'
import HlPilotRegions from '@/components/business/HlPilotRegions.vue'
import HlPilotStepper from '@/components/business/HlPilotStepper.vue'

export interface ComponentRegistration {
  meta: ComponentMeta
  component: Component
}

class StructuredComponentRegistry {
  private registrations = new Map<string, ComponentRegistration>()

  constructor() {
    this.initBuiltinComponents()
  }

  private initBuiltinComponents(): void {
    // ────────────── P0 通用核心组件 ──────────────
    this.register(
      {
        componentId: 'metric-grid',
        nameKey: 'structured.components.metricGrid',
        descriptionKey: 'structured.components.metricGrid',
        supportedDensities: ['compact', 'standard', 'expanded'],
        supportedHosts: ['pilot-drawer', 'pilot-page', 'view-page', 'dashboard'],
        schemaType: 'MetricGridData',
      },
      MetricGrid,
    )

    this.register(
      {
        componentId: 'data-table',
        nameKey: 'structured.components.dataTable',
        descriptionKey: 'structured.components.dataTable',
        supportedDensities: ['compact', 'standard', 'expanded'],
        supportedHosts: ['pilot-drawer', 'pilot-page', 'view-page', 'dashboard'],
        schemaType: 'DataTableData',
      },
      DataTable,
    )

    this.register(
      {
        componentId: 'time-series',
        nameKey: 'structured.components.timeSeries',
        descriptionKey: 'structured.components.timeSeries',
        supportedDensities: ['compact', 'standard', 'expanded'],
        supportedHosts: ['pilot-drawer', 'pilot-page', 'view-page', 'dashboard'],
        schemaType: 'TimeSeriesData',
      },
      TimeSeries,
    )

    this.register(
      {
        componentId: 'coverage-matrix',
        nameKey: 'structured.components.coverageMatrix',
        descriptionKey: 'structured.components.coverageMatrix',
        supportedDensities: ['compact', 'standard', 'expanded'],
        supportedHosts: ['pilot-drawer', 'pilot-page', 'view-page', 'dashboard'],
        schemaType: 'CoverageMatrixData',
      },
      CoverageMatrix,
    )

    this.register(
      {
        componentId: 'status-board',
        nameKey: 'structured.components.statusBoard',
        descriptionKey: 'structured.components.statusBoard',
        supportedDensities: ['compact', 'standard', 'expanded'],
        supportedHosts: ['pilot-drawer', 'pilot-page', 'view-page', 'dashboard'],
        schemaType: 'StatusBoardData',
      },
      StatusBoard,
    )

    // ────────────── 既有业务卡种（Legacy 挂载兼容） ──────────────
    this.register(
      {
        componentId: 'legacy-price',
        nameKey: 'structured.components.legacyPrice',
        descriptionKey: 'structured.components.legacyPrice',
        supportedDensities: ['standard', 'compact'],
        supportedHosts: ['pilot-drawer', 'pilot-page'],
        schemaType: 'PilotPriceFacts',
      },
      PriceFactsCard,
    )

    this.register(
      {
        componentId: 'legacy-games',
        nameKey: 'structured.components.legacyGames',
        descriptionKey: 'structured.components.legacyGames',
        supportedDensities: ['standard', 'compact'],
        supportedHosts: ['pilot-drawer', 'pilot-page'],
        schemaType: 'PilotGameFacts',
      },
      GamesFactsCard,
    )

    this.register(
      {
        componentId: 'legacy-action',
        nameKey: 'structured.components.legacyAction',
        descriptionKey: 'structured.components.legacyAction',
        supportedDensities: ['compact', 'standard'],
        supportedHosts: ['pilot-drawer', 'pilot-page'],
        schemaType: 'PilotActionFacts',
      },
      ActionFactsCard,
    )

    this.register(
      {
        componentId: 'legacy-navigate',
        nameKey: 'structured.components.legacyNavigate',
        descriptionKey: 'structured.components.legacyNavigate',
        supportedDensities: ['compact', 'standard'],
        supportedHosts: ['pilot-drawer', 'pilot-page'],
        schemaType: 'PilotNavigateFacts',
      },
      NavigateFactsCard,
    )

    this.register(
      {
        componentId: 'legacy-rows',
        nameKey: 'structured.components.legacyRows',
        descriptionKey: 'structured.components.legacyRows',
        supportedDensities: ['compact', 'standard'],
        supportedHosts: ['pilot-drawer', 'pilot-page'],
        schemaType: 'PilotRowsFacts',
      },
      RowsFactsCard,
    )

    this.register(
      {
        componentId: 'legacy-family',
        nameKey: 'structured.components.legacyFamily',
        descriptionKey: 'structured.components.legacyFamily',
        supportedDensities: ['standard', 'compact'],
        supportedHosts: ['pilot-drawer', 'pilot-page'],
        schemaType: 'PilotFamilyFacts',
      },
      FamilyFactsCard,
    )

    this.register(
      {
        componentId: 'legacy-achievements',
        nameKey: 'structured.components.legacyAchievements',
        descriptionKey: 'structured.components.legacyAchievements',
        supportedDensities: ['standard', 'compact'],
        supportedHosts: ['pilot-drawer', 'pilot-page'],
        schemaType: 'PilotAchievementFacts',
      },
      AchievementsFactsCard,
    )

    this.register(
      {
        componentId: 'legacy-proposal',
        nameKey: 'structured.components.legacyProposal',
        descriptionKey: 'structured.components.legacyProposal',
        supportedDensities: ['standard', 'compact'],
        supportedHosts: ['pilot-drawer', 'pilot-page'],
        schemaType: 'PilotProposalFacts',
      },
      ProposalFactsCard,
    )

    this.register(
      {
        componentId: 'legacy-regions',
        nameKey: 'structured.components.legacyRegions',
        descriptionKey: 'structured.components.legacyRegions',
        supportedDensities: ['standard', 'expanded'],
        supportedHosts: ['pilot-drawer', 'pilot-page'],
        schemaType: 'PilotRegionsFacts',
      },
      HlPilotRegions,
    )

    this.register(
      {
        componentId: 'legacy-compare',
        nameKey: 'structured.components.legacyCompare',
        descriptionKey: 'structured.components.legacyCompare',
        supportedDensities: ['standard', 'expanded'],
        supportedHosts: ['pilot-drawer', 'pilot-page'],
        schemaType: 'PilotCompareFacts',
      },
      HlPilotCompare,
    )

    this.register(
      {
        componentId: 'legacy-stepper',
        nameKey: 'structured.components.legacyStepper',
        descriptionKey: 'structured.components.legacyStepper',
        supportedDensities: ['standard', 'compact'],
        supportedHosts: ['pilot-drawer', 'pilot-page'],
        schemaType: 'PilotStepperFacts',
      },
      HlPilotStepper,
    )

    this.register(
      {
        componentId: 'legacy-epic',
        nameKey: 'structured.components.legacyEpic',
        descriptionKey: 'structured.components.legacyEpic',
        supportedDensities: ['standard', 'compact'],
        supportedHosts: ['pilot-drawer', 'pilot-page'],
        schemaType: 'PilotEpicFacts',
      },
      HlPilotEpic,
    )
  }

  /** 注册展示组件 */
  register(meta: ComponentMeta, component: Component): void {
    if (this.registrations.has(meta.componentId)) {
      console.warn(`[HSUI] Component ${meta.componentId} is already registered. Overwriting.`)
    }
    this.registrations.set(meta.componentId, { meta, component })
  }

  /** 获取组件渲染器 */
  getComponent(componentId: string): Component | undefined {
    return this.registrations.get(componentId)?.component
  }
}

export const registry = new StructuredComponentRegistry()

/**
 * 校验并确保 StructuredBlock 实例格式有效
 */
export function validateBlock(block: unknown): block is StructuredBlock {
  if (!block || typeof block !== 'object') return false
  const b = block as Record<string, unknown>
  if (typeof b.id !== 'string' || !b.id) return false
  if (typeof b.componentId !== 'string' || !b.componentId) return false
  if (!b.status) return false
  return true
}
