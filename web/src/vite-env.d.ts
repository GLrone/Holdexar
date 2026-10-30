/// <reference types="vite/client" />

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<Record<string, never>, Record<string, never>, unknown>
  export default component
}

declare module 'markdown-it-mark' {
  import type { PluginSimple } from 'markdown-it'
  const mark: PluginSimple
  export default mark
}
