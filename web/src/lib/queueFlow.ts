/**
 * 任务队列组成卡（HlQueueFlow）的段定义与派生逻辑。
 *
 * 数据源唯一：GET /crawl/queue-specs（后端组队函数经逐段开关过滤后的段名
 * 序列，顺序即真实组队顺序）——开关态与组成都从它派生，前端不自行推导
 * 「哪段在哪种代理策略下出现」。
 */

export interface QueueSegmentSpec {
  key: 'missing' | 'pool' | 'discounts' | 'catalog' | 'specials'
  /** 词条 key（组件渲染期 t()，模块级常量只存 key） */
  nameKey: string
  descKey: string
  /** 开关写入的设置字段；null = 无独立开关（特惠榜尾段跟随目录层） */
  setting: 'queue_missing' | 'queue_pool' | 'queue_discounts' | 'catalog_refresh' | null
}

export const QUEUE_SEGMENT_SPECS: QueueSegmentSpec[] = [
  {
    key: 'missing',
    nameKey: 'crawl.queue.seg.missing',
    descKey: 'crawl.queue.seg.missingDesc',
    setting: 'queue_missing',
  },
  {
    key: 'pool',
    nameKey: 'crawl.queue.seg.pool',
    descKey: 'crawl.queue.seg.poolDesc',
    setting: 'queue_pool',
  },
  {
    key: 'discounts',
    nameKey: 'crawl.queue.seg.discounts',
    descKey: 'crawl.queue.seg.discountsDesc',
    setting: 'queue_discounts',
  },
  {
    key: 'catalog',
    nameKey: 'crawl.queue.seg.catalog',
    descKey: 'crawl.queue.seg.catalogDesc',
    setting: 'catalog_refresh',
  },
  {
    key: 'specials',
    nameKey: 'crawl.queue.seg.specials',
    descKey: 'crawl.queue.seg.specialsDesc',
    setting: null,
  },
]

export interface QueueSegmentView extends QueueSegmentSpec {
  /** 是否在下轮组成里（False = 灰色停用态） */
  on: boolean
}

/** 来源框视图：固定段定义顺序，on = 段名出现在返回序列里 */
export function resolveQueueSegments(specNames: readonly string[]): QueueSegmentView[] {
  const present = new Set(specNames)
  return QUEUE_SEGMENT_SPECS.map((spec) => ({ ...spec, on: present.has(spec.key) }))
}

/** 总队列链：返回序列原样投影（真实组队顺序），未知段名忽略 */
export function resolveQueueChain(specNames: readonly string[]): QueueSegmentView[] {
  const byKey = new Map(QUEUE_SEGMENT_SPECS.map((spec) => [spec.key, spec]))
  const chain: QueueSegmentView[] = []
  for (const name of specNames) {
    const spec = byKey.get(name as QueueSegmentSpec['key'])
    if (spec) chain.push({ ...spec, on: true })
  }
  return chain
}
