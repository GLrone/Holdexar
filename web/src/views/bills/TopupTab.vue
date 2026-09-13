<script setup lang="ts">
/* 充值流水：口径条 + 币种卡 + 按年分组的流水行（紫色调，区别于消费台账）。 */
import { computed, onMounted, ref } from 'vue'

import { HlEmpty, HlSkeleton, message } from '@/components/ui'
import { billsApi, type BillSummary, type BillTopupItem } from '@/api/client'
import { currencyName } from '@/api/currencies'
import { cachedGet } from '@/lib/apiCache'
import { useI18n, useLocaleFormat } from '@/locales'

// 金额格式化统一出口（原为写死 'zh-CN'，英文界面下不跟随）
const fmt = useLocaleFormat()
const { t } = useI18n()

const props = defineProps<{
  importId: number
  byCurrency: { currency: string; total: number; cnyFen: number; count: number }[]
  summary: BillSummary
}>()

const rows = ref<BillTopupItem[]>([])
const loading = ref(false)

async function load(force = false) {
  loading.value = true
  try {
    // 走 cachedGet：本 tab 由 v-if 切换，每次切回来都会重挂载（2000 行/次）
    const res = await cachedGet(
      `bills.topup:${props.importId}`,
      () => billsApi.topupTxs(props.importId, 2000),
      { force },
    )
    rows.value = res.rows
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    loading.value = false
  }
}

/* 按年分组 */
interface YearGroup {
  year: string
  netFen: number
  rows: BillTopupItem[]
}
const groups = computed<YearGroup[]>(() => {
  const map = new Map<string, YearGroup>()
  for (const tx of rows.value) {
    const year = tx.date.slice(0, 4)
    let g = map.get(year)
    if (!g) {
      g = { year, netFen: 0, rows: [] }
      map.set(year, g)
    }
    g.netFen += tx.cnyFen ?? 0
    g.rows.push(tx)
  }
  return [...map.values()].sort((a, b) => b.year.localeCompare(a.year))
})

const fmtFen = (fen: number | null) => (fen == null ? '—' : fmt.money(fen / 100))
const fmtCur = (tx: BillTopupItem) =>
  `${tx.sign < 0 ? '+' : ''}${fmt.round2(tx.amount)} ${tx.currency}`

onMounted(() => load())
</script>

<template>
  <div>
    <div class="bills-mini">
      <div class="bills-mini__item">{{ t('bills.topup.stats.total') }}<b class="mono">¥{{ fmtFen(summary.topupSpendFen) }}</b></div>
      <div class="bills-mini__item">{{ t('bills.topup.stats.refund') }}<b class="mono" style="color: var(--danger)">-¥{{ fmtFen(summary.topupRefundFen) }}</b></div>
      <div class="bills-mini__item">{{ t('bills.topup.stats.net') }}<b class="mono" style="color: #8b5cf6">¥{{ fmtFen(summary.topupNetFen) }}</b></div>
      <div class="bills-mini__item">{{ t('bills.topup.stats.currencies') }}<b class="mono">{{ byCurrency.length }}</b></div>
    </div>

    <div v-if="byCurrency.length" class="bills-cur-grid">
      <div v-for="c in byCurrency" :key="c.currency" class="bills-cur-card">
        <div class="bills-cur-card__cur">{{ t('bills.currencyValue', { name: currencyName(c.currency), code: c.currency }) }}</div>
        <div class="bills-cur-card__num">{{ fmt.round2(c.total) }} {{ c.currency }}</div>
        <div class="bills-cur-card__sub">{{ t('bills.topup.card.sub', { amount: fmtFen(c.cnyFen), count: c.count }) }}</div>
      </div>
    </div>

    <!-- 加载态曾是一片空白；骨架屏三组「年汇总 + 明细行」，与真实结构同形 -->
    <HlSkeleton v-if="loading" variant="text" :count="3" :rows="4" />
    <HlEmpty v-else-if="!rows.length" icon="💰" :text="t('bills.topup.empty')" />

    <div v-for="g in groups" :key="g.year" class="bills-year">
      <div class="bills-year__head" style="cursor: default">
        <span class="bills-year__title">{{ g.year }}</span>
        <!-- 整句一条词条（行内 <b> 在词条值里），v-html 渲染——同 LedgerTab 年汇总行 -->
        <span
          class="bills-year__sum"
          v-html="t('bills.topup.year.sum', { amount: fmtFen(g.netFen), count: g.rows.length })"
        ></span>
      </div>
      <div v-for="tx in g.rows" :key="tx.id" class="bills-tx" :class="{ 'is-refund': tx.isRefund }">
        <div class="bills-tx__row" style="cursor: default">
          <span class="bills-tx__date">{{ tx.date.slice(5) }}</span>
          <span class="bills-tx__desc">{{ tx.desc || t('bills.topup.desc.default') }}</span>
          <span class="bills-tx__badges">
            <span v-if="tx.isRefund" class="bills-badge bills-badge--refund">{{ t('bills.topup.badge.refund') }}</span>
          </span>
          <span class="bills-tx__cur">{{ fmtCur(tx) }}</span>
          <span class="bills-tx__cny" :class="{ 'is-missing': tx.cnyFen == null }">
            <template v-if="tx.cnyFen != null">¥{{ fmtFen(tx.cnyFen) }}</template>
            <template v-else>{{ tx.fxNote || t('bills.fxMissing') }}</template>
          </span>
        </div>
      </div>
    </div>
  </div>
</template>
