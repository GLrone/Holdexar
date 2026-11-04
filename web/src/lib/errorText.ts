/**
 * 异常 → 用户可读文案：catch 出口的统一收敛层（message.error(errorText(e))）。
 *
 * 已知类别（网络/401/403/5xx）映射词典词条（结果+影响+下一步）；其余错误保留
 * 原始 message 但加统一前缀——不吞异常细节，高级用户仍可从后半句排障。
 */
import { ApiError } from '@/api/client'
import { useI18n } from '@/locales'

/** 浏览器 fetch 网络失败的典型词面（Chrome/Firefox/Safari/本地代理报错）。 */
const NETWORK_RE =
  /failed to fetch|networkerror|load failed|network request failed|err_(name_not_resolved|connection)/i

function isAbort(cause: unknown): boolean {
  return cause instanceof DOMException && cause.name === 'AbortError'
}

export function errorText(cause: unknown): string {
  const { t } = useI18n()
  if (cause instanceof ApiError) {
    if (cause.status === 401) return t('common.error.auth')
    if (cause.status === 403) return t('common.error.forbidden')
    if (cause.status >= 500) return t('common.error.server')
    // detail 只有「HTTP xxx」= 后端没给可读说明，直出无信息量 → 请求失败词条
    if (/^HTTP \d+$/.test(cause.message)) return t('common.error.request')
    // 其余 4xx 的 detail 多为后端用户语言（校验/配置错误），前缀后保留
    return t('common.error.prefix') + cause.message
  }
  const message = cause instanceof Error ? cause.message : String(cause)
  if (isAbort(cause) || NETWORK_RE.test(message)) return t('common.error.network')
  const detail = message.trim()
  return detail ? t('common.error.prefix') + detail : t('common.error.request')
}
