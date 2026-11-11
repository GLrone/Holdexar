import { request } from '@/api/client'

export interface PluginField {
  type: 'string' | 'boolean' | 'number'
  title?: string
  format?: string
  enum?: Array<string | number | boolean>
}

export interface PluginManifest {
  id: string
  name: string
  version: string
  description: string
  author: string
  homepage?: string
  apiVersion: number
  kind: string
  domains: string[]
  configSchema?: { type: string; properties: Record<string, PluginField>; required?: string[] }
}

export interface PluginView {
  manifest: PluginManifest
  enabled: boolean
  configured: boolean
  configuredSecrets?: string[]
  config?: Record<string, string | number | boolean>
  sha256: string
  url: string
  compatible: boolean
  trust: 'unsigned' | 'verified'
  error?: string
}

export interface PluginsState {
  builtin: Array<{ id: string; name?: string; enabled: boolean; running: boolean }>
  installed: PluginView[]
  catalog: PluginView[]
  catalogError?: string
}

export const pluginsChangedEvent = 'holdexar:plugins-changed'

export const pluginsApi = {
  list: () => request<PluginsState>('GET', '/plugins', undefined, { noCache: true }),
  discover: (source: string, q: string) => request<{ items: PluginView[]; cached: boolean; error?: string }>(
    'GET', `/plugins/discover?${new URLSearchParams({ source, q })}`, undefined, { noCache: true },
  ),
  install: (plugin: PluginView) => request('POST', '/plugins/install', {
    id: plugin.manifest.id, version: plugin.manifest.version, sha256: plugin.sha256, url: plugin.url,
  }),
  installLocal: async (file: File) => request('POST', '/plugins/install-local', undefined, {
    rawBody: await file.arrayBuffer(), contentType: 'application/octet-stream',
  }),
  update: (id: string, body: { enabled?: boolean; config?: Record<string, unknown> }) =>
    request('PUT', `/plugins/${encodeURIComponent(id)}`, body),
  remove: (id: string) => request('DELETE', `/plugins/${encodeURIComponent(id)}`),
  test: (id: string) => request('POST', `/plugins/${encodeURIComponent(id)}/test`),
  builtin: (id: string, enabled: boolean) => request('PUT', `/plugins/builtin/${encodeURIComponent(id)}`, { enabled }),
}
