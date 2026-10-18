/**
 * Holdexar Structured UI System — 组件统一注册中心。
 *
 * 集中管理系统全部结构化展示组件的元数据、Schema 契约、渲染器绑定及 Agent 发现目录。
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
        agentDescription: 'Presents KPI numbers, coverage rates, proxy health scores, or bills/achievements summary with trends and benchmarks.',
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
        agentDescription: 'Renders structured tabular records with sorting, pagination, status pills, and row actions.',
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
        agentDescription: 'Renders step lines or smooth curves for price history, network latency, or system throughput over time.',
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
        agentDescription: '2D matrix showing multi-region crawl coverage, success/failed status, and reasons across games or nodes.',
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
        agentDescription: 'System status board for proxies, scheduler, write gate, updater, and background tasks.',
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
        agentDescription: 'Display single game price, discount, ATL benchmark, and 1-year median.',
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
        agentDescription: 'Display list of games with cover, rating, discount, and filter chips.',
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
        agentDescription: 'Receipt for completed actions like following or setting alerts.',
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
        agentDescription: 'Navigation guide to open specific app views.',
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
        agentDescription: 'Display diagnostic key-value rows.',
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
        agentDescription: 'Display Steam family group status and member quotas.',
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
        agentDescription: 'Display platinum achievements and recent unlocks.',
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
        agentDescription: 'Interactive proposal panel requiring user approval.',
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
        agentDescription: 'Display multi-region price comparison matrix.',
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
        agentDescription: 'Compare attributes and prices of multiple games side by side.',
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
        agentDescription: 'Multi-step workflow progress indicator.',
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
        agentDescription: 'Display Epic Games Store free promotions.',
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

  /** 获取组件元数据 */
  getMeta(componentId: string): ComponentMeta | undefined {
    return this.registrations.get(componentId)?.meta
  }

  /** 获取组件渲染器 */
  getComponent(componentId: string): Component | undefined {
    return this.registrations.get(componentId)?.component
  }

  /** 检查是否已注册 */
  has(componentId: string): boolean {
    return this.registrations.has(componentId)
  }

  /** 获取所有注册元数据 */
  getAllMeta(): ComponentMeta[] {
    return Array.from(this.registrations.values()).map((r) => r.meta)
  }

  /**
   * 导出供 Agent 自动发现与选择的轻量组件能力目录。
   */
  getAgentCatalog(): Array<{
    id: string
    description: string
    schemaType?: string
  }> {
    return this.getAllMeta().map((m) => ({
      id: m.componentId,
      description: m.agentDescription,
      schemaType: m.schemaType,
    }))
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
