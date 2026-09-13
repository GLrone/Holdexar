<script setup lang="ts">
import { computed, ref } from 'vue'
import HlIcon from './HlIcon.vue'

export interface HlTreeNode {
  label: string
  value?: string
  children?: HlTreeNode[]
}

interface FlatNode {
  node: HlTreeNode
  depth: number
  hasChildren: boolean
}

const props = withDefaults(defineProps<{ nodes: HlTreeNode[] }>(), { nodes: () => [] })

const emit = defineEmits<{ select: [node: HlTreeNode] }>()

const openSet = ref<Set<HlTreeNode>>(new Set())
const activeNode = ref<HlTreeNode | null>(null)

function toggle(node: HlTreeNode, hasChildren: boolean) {
  activeNode.value = node
  emit('select', node)
  if (hasChildren) {
    const next = new Set(openSet.value)
    if (next.has(node)) next.delete(node)
    else next.add(node)
    openSet.value = next
  }
}

/** 展开状态取反后扁平化渲染 */
const flat = computed(() => {
  const out: FlatNode[] = []
  const walk = (list: HlTreeNode[], depth: number) => {
    for (const node of list) {
      const hasChildren = !!node.children?.length
      out.push({ node, depth, hasChildren })
      if (hasChildren && openSet.value.has(node)) walk(node.children!, depth + 1)
    }
  }
  walk(props.nodes, 0)
  return out
})
</script>

<template>
  <div class="hl-tree">
    <div
      v-for="(f, i) in flat"
      :key="i"
      class="hl-tree-node"
      :class="{ 'is-on': activeNode === f.node, 'is-open': openSet.has(f.node) }"
      :style="{ paddingLeft: 8 + f.depth * 18 + 'px' }"
      @click="toggle(f.node, f.hasChildren)"
    >
      <i v-if="f.hasChildren" class="hl-tree-node__arrow">▸</i>
      <span v-else style="width: 12px" />
      {{ f.node.label }}
    </div>
  </div>
</template>
