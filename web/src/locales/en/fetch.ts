/* English dictionary · Auto Fetch page (mirrors zh-CN/fetch.ts key by key).
   Copy is written for first-time users: no implementation words. */

import type { MessageKey } from '../zh-CN'

const fetchPage: Partial<Record<MessageKey, string>> = {
  'fetch.desc':
    "Holdexar keeps the content below up to date for you over the network. Turn off whatever you don't need: data already fetched stays, and manual refresh keeps working.",
  'fetch.epic.label': 'Epic free games',
  'fetch.epic.desc':
    'The Epic Games Store often gives away free games. Keep this on and the home page shows what you can claim for free right now.',
  'fetch.hb.label': 'Humble Choice monthly bundle',
  'fetch.hb.desc':
    "Humble Choice is a monthly game bundle. Keep this on and each month's lineup is collected automatically, with a summary email.",
  'fetch.boards.label': 'Steam charts',
  'fetch.boards.desc':
    "Fetches Steam's top sellers, specials, new releases and coming-soon charts — the popular games on Find Games come from here.",
  'fetch.bundleCounts.label': 'Bundle records',
  'fetch.bundleCounts.desc':
    "Keeps track of which deal bundles each game has appeared in, shown on the Bundles page and game details. When off, these records stop updating.",
  'fetch.fx.label': 'Exchange rates',
  'fetch.fx.desc':
    'Fetches fresh exchange rates every night to convert foreign prices into CNY. When off, the latest snapshot is used; the Rates page can still refresh manually.',
  'fetch.fxHistory.label': 'Historical rates repair',
  'fetch.fxHistory.desc':
    'Fills missing historical rate days over the network so past prices convert more accurately. Uses a keyless public data source by default — no configuration needed.',
  'fetch.interval.label': 'Price update interval',
  'fetch.interval.desc':
    'How often game prices refresh automatically. Shorter means fresher prices but more network traffic. No effect while automatic price updates are off on the Tasks page.',
  'fetch.interval.hours': 'Every {n} h',
  'fetch.interval.toast': 'Updated: prices now refresh every {n} hours',
  'fetch.reset': 'Reset to defaults',
  'fetch.reset.toast': 'Defaults restored: all sources on, prices refresh every 6 hours',
  'fetch.toast.on': 'Automatic updates turned on',
  'fetch.toast.off': 'Turned off: no more automatic updates, existing data stays',
  'fetch.toast.failed': "Couldn't save, please try again",
}

export default fetchPage
