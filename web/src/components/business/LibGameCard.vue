<script setup lang="ts">
/**
 * 游戏库卡片 —— gamelib 页（账号游戏库 / 家庭库）统一的游戏展示卡。
 *
 * 形态（相对 family 旧 lib-card 的重排版）：
 * - 封面升为主视觉：16:9（Steam header 460×215 原比例）整幅铺满卡宽，
 *   旧卡 78px 高的裁切条不再出现；
 * - 文案区两层：名称一行 + 「类型 · 发行年」弱化行，元信息不再逐行排；
 * - 底行左右分置：价格（含划线原价/免费/暂无价格三态）与拥有者头像栈；
 * - 整卡 router-link 直达游戏详情，徽章（独占/共享/自定义）走 #badge 插槽
 *   由调用方决定，卡本体不掺业务语义。
 */
import { computed } from 'vue'

import { useI18n } from '@/locales'
import HlImg from '@/components/ui/HlImg.vue'

const props = withDefaults(
  defineProps<{
    appid: number
    name?: string | null
    headerImage?: string | null
    /** 逗号分隔的类型串（games.genres 原样），只展示首类型 */
    genres?: string | null
    releaseDate?: string | null
    /** CN 价 CNY 分；null = 本地未爬到价格 */
    priceFen?: number | null
    originalPriceFen?: number | null
    discount?: number
    /** 拥有者头像栈（≤4 展示，超出落 +n） */
    owners?: { name: string; url: string }[]
    /** 拥有者总数（大于 owners.length 时出 +n；缺省取 owners.length） */
    ownerTotal?: number
  }>(),
  {
    name: null,
    headerImage: null,
    genres: null,
    releaseDate: null,
    priceFen: null,
    originalPriceFen: null,
    discount: 0,
    owners: () => [],
    ownerTotal: undefined,
  },
)

const { t } = useI18n()

const displayName = computed(() => props.name?.trim() || `AppID ${props.appid}`)
/* 本地 games 表没爬到的游戏不给裂图：按 Steam 商店公开素材路径拼封面，
   404 时 HlImg 落首字符占位（game-detail 的 bundle 封面同款兜底思路） */
const cover = computed(
  () =>
    props.headerImage ||
    `https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/${props.appid}/header.jpg`,
)
const metaLine = computed(() => {
  const genre = (props.genres || '').split(',')[0]?.trim() || ''
  const year = (props.releaseDate || '').slice(0, 4)
  return [genre, year].filter(Boolean).join(' · ')
})

type PriceState = 'unknown' | 'free' | 'paid'
const priceState = computed<PriceState>(() => {
  if (props.priceFen === null) return 'unknown'
  if (props.priceFen === 0) return 'free'
  return 'paid'
})
const priceLabel = computed(() => {
  if (priceState.value === 'unknown') return t('gamelib.card.noPrice')
  if (priceState.value === 'free') return t('gamelib.card.free')
  return `¥${((props.priceFen ?? 0) / 100).toFixed(0)}`
})

const ownerMore = computed(() => {
  const total = props.ownerTotal ?? props.owners.length
  return Math.max(0, total - Math.min(props.owners.length, 4))
})
</script>

<template>
  <router-link :to="`/game/${appid}`" class="glc" :title="displayName">
    <div class="glc__cover">
      <HlImg :src="cover" :alt="displayName" loading="lazy" class="glc__img">
        <template #fallback>
          <span class="glc__fallback">{{ displayName.slice(0, 2) }}</span>
        </template>
      </HlImg>
      <span v-if="discount > 0" class="glc__discount">-{{ discount }}%</span>
      <slot name="badge" />
    </div>
    <div class="glc__body">
      <div class="glc__name">{{ displayName }}</div>
      <div class="glc__meta" :class="{ 'is-empty': !metaLine }">{{ metaLine || '—' }}</div>
      <slot name="extra" />
      <div class="glc__foot">
        <span
          class="glc__price"
          :class="{ 'is-free': priceState === 'free', 'is-unknown': priceState === 'unknown' }"
        >
          <s v-if="priceState === 'paid' && discount > 0 && originalPriceFen" class="glc__orig">
            ¥{{ ((originalPriceFen ?? 0) / 100).toFixed(0) }}
          </s>
          {{ priceLabel }}
        </span>
        <span v-if="owners.length" class="glc__owners">
          <template v-for="o in owners.slice(0, 4)" :key="o.name + o.url">
            <img v-if="o.url" class="glc__ava" :src="o.url" :alt="o.name" :title="o.name" loading="lazy" />
            <span v-else class="glc__ava glc__ava--txt" :title="o.name">{{ o.name.slice(0, 1) }}</span>
          </template>
          <span v-if="ownerMore > 0" class="glc__more">+{{ ownerMore }}</span>
        </span>
      </div>
    </div>
  </router-link>
</template>

<style scoped>
.glc {
  display: flex;
  flex-direction: column;
  background: var(--bg-card);
  border: 1px solid var(--line-1);
  border-radius: 10px;
  overflow: hidden;
  text-decoration: none;
  transition: var(--transition);
}
.glc:hover {
  border-color: var(--accent-a40);
  box-shadow: var(--shadow-md);
  transform: translateY(-3px);
}

/* 封面：Steam header 原比例整幅，网格居中兜底字 */
.glc__cover {
  position: relative;
  aspect-ratio: 460 / 215;
  display: grid;
  place-items: center;
  background: linear-gradient(135deg, var(--surface-chip-2), var(--surface-inset));
  overflow: hidden;
}
.glc__img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}
.glc__fallback {
  font-size: 22px;
  font-weight: 700;
  color: var(--text-dim);
  letter-spacing: 1px;
}
.glc__discount {
  position: absolute;
  top: 6px;
  left: 6px;
  font-size: 10px;
  font-weight: 700;
  padding: 2px 6px;
  border-radius: 4px;
  background: var(--success-a20);
  color: var(--success);
  font-variant-numeric: tabular-nums;
}
.glc__cover :deep(.glc-badge) {
  position: absolute;
  top: 6px;
  right: 6px;
  font-size: 9.5px;
  padding: 2px 6px;
  border-radius: 4px;
  font-weight: 700;
  backdrop-filter: blur(4px);
}

/* 文案区 */
.glc__body {
  padding: 8px 10px 9px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  flex: 1;
}
.glc__name {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.glc__meta {
  font-size: 10.5px;
  color: var(--text-dim);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.glc__meta.is-empty {
  opacity: 0.6;
}

/* 底行：价格左、头像栈右 */
.glc__foot {
  margin-top: auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  min-height: 18px;
}
.glc__price {
  font-size: 12.5px;
  font-weight: 700;
  color: var(--text-primary);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  display: inline-flex;
  align-items: baseline;
  gap: 5px;
}
.glc__price.is-free {
  color: var(--success);
}
.glc__price.is-unknown {
  color: var(--text-faint);
  font-weight: 500;
  font-size: 11px;
}
.glc__orig {
  font-size: 10px;
  font-weight: 500;
  color: var(--text-faint);
}
.glc__owners {
  display: inline-flex;
  align-items: center;
  flex-shrink: 0;
}
.glc__ava {
  width: 17px;
  height: 17px;
  border-radius: 50%;
  object-fit: cover;
  border: 1.5px solid var(--bg-card);
  flex-shrink: 0;
}
.glc__ava + .glc__ava,
.glc__ava + .glc__ava--txt,
.glc__ava--txt + .glc__ava {
  margin-left: -5px;
}
.glc__ava--txt {
  display: grid;
  place-items: center;
  font-size: 9px;
  font-weight: 700;
  color: var(--text-on-fill);
  background: var(--accent);
}
.glc__more {
  margin-left: 4px;
  font-size: 9.5px;
  font-weight: 600;
  color: var(--text-dim);
  font-variant-numeric: tabular-nums;
}
</style>
