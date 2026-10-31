import { ref } from 'vue'
import { useI18n } from '@/locales'
import { ApiError, type PilotSessionListItem } from '@/api/client'
import { message } from '@/components/ui'
import { pilotApi } from '@/api/client'

/** 会话行动作（行内改名 / 两段式删除 / 置顶 / 归档）：会话列与标签栏共用同一套语义。
    账本清单由调用方持有；本组合式函数只承载动作与防连点，成功后经回调交还账本。 */
export function useSessionRowActions(opts: {
  /** 账本清单（乐观回填与查找用） */
  sessions: () => PilotSessionListItem[] | null
  /** 动作成功后交账本重投影 */
  refresh: () => void
  /** 删除成功后交调用方清打开集/切会话 */
  deleted: (sid: string) => void
}) {
  const { t } = useI18n()

  const renamingSid = ref('')
  const renameText = ref('')
  const deleteArmSid = ref('')
  let deleteArmTimer: ReturnType<typeof setTimeout> | null = null
  // 置顶/归档/删除防连点；同名 sid 只压一次
  const flagBusy = ref('')

  /** 置顶/归档开关：账本追加记录，成功后回填本行并交账本重投影（排序随之后端定） */
  async function togglePin(it: PilotSessionListItem) {
    if (flagBusy.value) return
    flagBusy.value = it.sid
    try {
      const r = await pilotApi.pinSession(it.sid, !it.pinned)
      if (!r.ok) {
        message.error(t('pilot.session.flagFailed'))
        return
      }
      it.pinned = !it.pinned
      opts.refresh()
    } catch {
      message.error(t('pilot.session.flagFailed'))
    } finally {
      flagBusy.value = ''
    }
  }

  async function toggleArchive(it: PilotSessionListItem) {
    if (flagBusy.value) return
    flagBusy.value = it.sid
    try {
      const r = await pilotApi.archiveSession(it.sid, !it.archived)
      if (!r.ok) {
        message.error(t('pilot.session.flagFailed'))
        return
      }
      it.archived = !it.archived
      opts.refresh()
    } catch {
      message.error(t('pilot.session.flagFailed'))
    } finally {
      flagBusy.value = ''
    }
  }

  function startRename(it: PilotSessionListItem) {
    renamingSid.value = it.sid
    renameText.value = it.title
  }

  async function submitRename() {
    const sid = renamingSid.value
    const text = renameText.value.trim()
    renamingSid.value = ''
    if (!sid || !text) return
    try {
      await pilotApi.renameSession(sid, text)
      const item = (opts.sessions() ?? []).find((x) => x.sid === sid)
      if (item) item.title = text
    } catch {
      message.error(t('pilot.history.renameFailed'))
    }
  }

  /** 两段式删除：首点武装（4s 内再点执行），防误删 */
  function armDelete(sid: string) {
    if (deleteArmSid.value === sid) {
      void removeSession(sid)
      return
    }
    deleteArmSid.value = sid
    if (deleteArmTimer) clearTimeout(deleteArmTimer)
    deleteArmTimer = setTimeout(() => {
      deleteArmSid.value = ''
    }, 4000)
  }

  async function removeSession(sid: string) {
    deleteArmSid.value = ''
    if (deleteArmTimer) clearTimeout(deleteArmTimer)
    try {
      await pilotApi.deleteSession(sid)
    } catch (e) {
      // 404 = 账本本无此会话（竞态/TTL 清扫）：视为已删；其余失败如实报错
      if (!(e instanceof ApiError && e.status === 404)) {
        message.error(t('pilot.history.deleteFailed'))
        return
      }
    }
    opts.deleted(sid)
  }

  return { renamingSid, renameText, deleteArmSid, flagBusy, startRename, submitRename, armDelete, togglePin, toggleArchive }
}
