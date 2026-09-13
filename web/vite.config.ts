import { existsSync } from 'node:fs'
import { fileURLToPath, URL } from 'node:url'
import { dirname, isAbsolute, resolve, sep } from 'node:path'

import tailwindcss from '@tailwindcss/vite'
import vue from '@vitejs/plugin-vue'
import { defineConfig, type Plugin } from 'vite'

/**
 * 工作树防线——路由先于视图落地时构建不再整体失败。
 *
 * 背景：router 挂了路由但 views 文件未建时，vite 静态解析 import
 * 会直接炸掉构建——本插件把缺失视图顶替为占位页，范围仅限 views 目录。
 * 本插件把 src/views/** 下缺失的 .vue 引用自动顶替为
 * src/views/_stub/Missing.vue「施工中」占位，并打印构建警告。
 *
 * 边界：只顶替 views 目录的 .vue——其他位置的缺失引用（组件/工具笔误）
 * 照常报错，不被静默掩盖。提交层防线见 npm run check:routes
 * （悬空引用禁止入库，vite.config.ts build 链 + .githooks/pre-commit）。
 */
function stubMissingViews(): Plugin {
  const srcDir = fileURLToPath(new URL('./src', import.meta.url))
  const stubAbs = resolve(srcDir, 'views/_stub/Missing.vue')

  const isViewPath = (p: string): boolean => {
    const norm = p.split(sep).join('/')
    return norm.includes('/src/views/') && norm.endsWith('.vue') && !norm.includes('/src/views/_stub/')
  }

  return {
    name: 'holdexar:stub-missing-views',
    enforce: 'pre',
    resolveId(source, importer) {
      if (!source.endsWith('.vue')) return null
      const candidates: string[] = []
      if (isAbsolute(source)) candidates.push(source)
      if (source.startsWith('@/')) candidates.push(resolve(srcDir, source.slice(2)))
      if (importer && (source.startsWith('./') || source.startsWith('../')))
        candidates.push(resolve(dirname(importer), source))
      const target = candidates.find(isViewPath)
      if (!target || existsSync(target)) return null
      this.warn(`[stub-views] 视图缺失，已用「施工中」占位顶替: ${source}`)
      return stubAbs
    },
  }
}

export default defineConfig({
  plugins: [stubMissingViews(), vue(), tailwindcss()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      // 并行验证可 VITE_PROXY_TARGET 覆盖（默认 28765 主后端不动）
      '/api': process.env.VITE_PROXY_TARGET || 'http://127.0.0.1:28765',
    },
  },
  build: {
    outDir: 'dist',
  },
})
