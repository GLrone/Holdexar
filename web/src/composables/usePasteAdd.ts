import { ref, type Ref } from 'vue'

import { crawlApi } from '@/api/client'
import { parseAppRefs } from '@/lib/appidRefs'
import { message } from '@/components/ui'
import { useI18n } from '@/locales'

/** 粘贴添加游戏链（找游戏空态与仪表盘欢迎卡共用）：
 *  解析粘贴文本 → 批量导入（只进目录，不建监控关系）→ 对新导入立刻取一次价格。
 *  导入不需要 Steam 账号；失败只报结果与出路，不把后端原文甩给用户。
 *  任务占用导致的取价失败不打断用户——价格由后续周期补齐。 */
export function usePasteAdd(handlers: {
  /** 有新导入（added > 0）时回调：调用方据此切「正在获取价格」等状态 */
  onAdded?: (added: number) => void
  /** 一次提交有结果（新增或全部已在目录）后的收尾：调用方重取列表 */
  onSettled: () => void | Promise<void>
}) {
  const { t } = useI18n()
  const addOpen: Ref<boolean> = ref(false)
  const addText = ref('')
  const addMsg = ref('')
  const addBusy = ref(false)

  async function submitAdd() {
    const text = addText.value.trim()
    if (!text || addBusy.value) return
    const { appids } = parseAppRefs(text)
    if (!appids.length) {
      addMsg.value = t('dashboard.welcome.addNone')
      return
    }
    addBusy.value = true
    addMsg.value = ''
    try {
      let added = 0
      let owned = 0
      const newIds: number[] = []
      for (let i = 0; i < appids.length; i += 100) {
        const r = await crawlApi.importApps(appids.slice(i, i + 100))
        added += r.ok
        owned += r.own
        newIds.push(
          ...r.results
            .filter((it) => it.status === 'ok' && it.appid)
            .map((it) => it.appid as number),
        )
      }
      if (newIds.length) {
        try {
          // 新导入直接触发一次获取（与批量导入同款语义）；任务占用则留给后续刷新
          await crawlApi.run('appids', newIds, 'import')
        } catch {
          /* 任务占用：不打断用户，价格由后续刷新补齐 */
        }
      }
      if (added > 0) {
        message.success(t('dashboard.welcome.added', { n: added }))
        addText.value = ''
        addOpen.value = false
        handlers.onAdded?.(added)
        await handlers.onSettled()
      } else if (owned > 0) {
        addMsg.value = t('dashboard.welcome.addOwned', { n: owned })
        await handlers.onSettled()
      } else {
        addMsg.value = t('dashboard.welcome.addNone')
      }
    } catch {
      addMsg.value = t('dashboard.welcome.addFailed')
    } finally {
      addBusy.value = false
    }
  }

  return { addOpen, addText, addMsg, addBusy, submitAdd }
}
