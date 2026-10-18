<script setup lang="ts">
import { computed } from 'vue'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart } from 'echarts/charts'
import { GridComponent, MarkLineComponent, MarkPointComponent, TooltipComponent } from 'echarts/components'
import VChart from 'vue-echarts'

import {
  axisPointerStyle,
  useChartPalette,
  useTipPalette,
  withAlpha,
} from '@/api/chartTheme'
import { useI18n } from '@/locales'
import type { StructuredDensity, TimeSeriesData } from '../types'
import HlEmpty from '@/components/ui/HlEmpty.vue'

use([CanvasRenderer, LineChart, GridComponent, TooltipComponent, MarkPointComponent, MarkLineComponent])

const props = withDefaults(
  defineProps<{
    data: TimeSeriesData
    density?: StructuredDensity
    height?: string
  }>(),
  {
    density: 'standard',
  },
)

const { t } = useI18n()
const palette = useChartPalette()
const tipPalette = useTipPalette()

const chartHeight = computed(() => {
  if (props.height) return props.height
  if (props.density === 'compact') return '160px'
  if (props.density === 'expanded') return '360px'
  return '240px'
})

const hasData = computed(() => {
  const lines = props.data?.lines ?? []
  return lines.length > 0 && lines.some((l) => (l.data?.length ?? 0) > 0)
})

function formatTs(val: number | string): string {
  const d = new Date(typeof val === 'number' ? val : val.replace(' ', 'T'))
  if (Number.isNaN(d.getTime())) return String(val)
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const date = String(d.getDate()).padStart(2, '0')
  return `${m}-${date}`
}

function parsePointTs(pointTimestamp: number | string): number {
  if (typeof pointTimestamp === 'number') return pointTimestamp
  return new Date(pointTimestamp.replace(' ', 'T')).getTime()
}

interface EChartsSeriesItem {
  name: string
  type: string
  step: string | boolean
  smooth: boolean
  showSymbol: boolean
  data: [number, number][]
  lineStyle: {
    color: string
    width: number
  }
  itemStyle: {
    color: string
  }
  areaStyle?: {
    color: string
  }
  animation: boolean
  markLine?: Record<string, unknown>
  markPoint?: Record<string, unknown>
}

const chartOption = computed(() => {
  const pal = palette.value
  const tip = tipPalette.value
  const lines = props.data?.lines ?? []
  const markers = props.data?.markers ?? []
  const isCompact = props.density === 'compact'

  const seriesList: EChartsSeriesItem[] = lines.map((line, idx) => {
    let strokeColor = pal.line
    if (line.tone === 'success') strokeColor = pal.success
    else if (line.tone === 'warn') strokeColor = pal.warning
    else if (line.tone === 'danger') strokeColor = pal.negative
    else if (idx === 1) strokeColor = pal.success
    else if (idx === 2) strokeColor = pal.warning

    const points: [number, number][] = (line.data || [])
      .map((p): [number, number] => [parsePointTs(p.t), p.v])
      .filter(([ts, v]) => Number.isFinite(ts) && Number.isFinite(v))

    const isStep = line.curveType === 'step'
    const isSmooth = line.curveType === 'smooth'

    return {
      name: line.name || `Series ${idx + 1}`,
      type: 'line',
      step: isStep ? 'end' : false,
      smooth: isSmooth,
      showSymbol: false,
      data: points,
      lineStyle: {
        color: strokeColor,
        width: isCompact ? 1.5 : 2,
      },
      itemStyle: {
        color: strokeColor,
      },
      areaStyle: props.data?.showArea
        ? {
            color: withAlpha(strokeColor, 0.12),
          }
        : undefined,
      animation: false,
    }
  })

  // 极值标记与水平阈值线
  if (markers.length > 0 && seriesList.length > 0) {
    const markLines: Record<string, unknown>[] = []
    const markPoints: Record<string, unknown>[] = []

    for (const m of markers) {
      let mColor = pal.warning
      if (m.tone === 'success') mColor = pal.success
      else if (m.tone === 'danger') mColor = pal.negative
      else if (m.tone === 'accent') mColor = pal.line

      if (m.type === 'horizontal-line' && typeof m.value === 'number') {
        markLines.push({
          yAxis: m.value,
          name: m.label,
          lineStyle: {
            color: mColor,
            type: 'dashed',
            width: 1.2,
          },
          label: {
            formatter: `${m.label}: ${m.value}`,
            position: 'insideEndTop',
            color: mColor,
            fontSize: 10,
          },
        })
      } else if (m.type === 'min' || m.type === 'max') {
        markPoints.push({
          type: m.type,
          name: m.label,
          itemStyle: { color: mColor },
          label: {
            color: tip.text,
            fontSize: 10,
          },
        })
      }
    }

    if (markLines.length > 0) {
      seriesList[0].markLine = {
        data: markLines,
        symbol: 'none',
        animation: false,
      }
    }
    if (markPoints.length > 0) {
      seriesList[0].markPoint = {
        data: markPoints,
        animation: false,
      }
    }
  }

  return {
    animation: false,
    axisPointer: axisPointerStyle(pal),
    grid: {
      left: isCompact ? 36 : 46,
      right: isCompact ? 12 : 20,
      top: isCompact ? 16 : 24,
      bottom: isCompact ? 24 : 32,
      containLabel: false,
    },
    tooltip: {
      trigger: 'axis',
      backgroundColor: tip.bg,
      borderColor: tip.border,
      borderWidth: 1,
      extraCssText: `box-shadow: ${tip.shadow}; border-radius: 8px;`,
      textStyle: {
        color: tip.text,
        fontSize: 12,
      },
      axisPointer: axisPointerStyle(pal),
    },
    xAxis: {
      type: 'time',
      axisLine: { lineStyle: { color: pal.axis } },
      axisTick: { show: false },
      axisLabel: {
        color: pal.label,
        fontSize: isCompact ? 10 : 11,
        formatter: (val: number) => formatTs(val),
      },
      splitLine: { show: false },
    },
    yAxis: {
      type: 'value',
      min: props.data?.yAxisMin ?? 'dataMin',
      max: props.data?.yAxisMax ?? 'dataMax',
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: {
        color: pal.label,
        fontSize: isCompact ? 10 : 11,
      },
      splitLine: {
        show: true,
        lineStyle: {
          color: pal.grid,
          type: 'dashed',
        },
      },
    },
    series: seriesList,
  }
})
</script>

<template>
  <div class="hsui-time-series" :class="`is-density-${density}`">
    <div v-if="!hasData" class="hsui-time-series__empty">
      <HlEmpty :description="t('structured.timeSeries.empty')" />
    </div>

    <div v-else class="hsui-time-series__chart-wrapper" :style="{ height: chartHeight }">
      <VChart :option="chartOption" autoresize class="hsui-time-series__canvas" />
    </div>
  </div>
</template>

<style scoped>
.hsui-time-series {
  width: 100%;
  display: flex;
  flex-direction: column;
}

.hsui-time-series__empty {
  padding: 30px 0;
}

.hsui-time-series__chart-wrapper {
  width: 100%;
  position: relative;
}

.hsui-time-series__canvas {
  width: 100%;
  height: 100%;
}
</style>
