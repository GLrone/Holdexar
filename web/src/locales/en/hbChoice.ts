/* hbChoice entries — Humble Choice monthly card (components/business/HbChoiceCards.vue).

   The month label (e.g. "HB慈善包26年9月包") is factual data from the API,
   not UI copy; card title and the two official links are full-sentence keys. */

const hbChoice = {
  'hbChoice.title': 'Humble Choice',
  'hbChoice.refresh': 'Refresh',
  'hbChoice.refreshing': 'Refreshing…',
  'hbChoice.refreshFailed': 'Refresh failed',
  'hbChoice.goMonth': 'Open bundle',
  'hbChoice.skip': 'Skip month',
  'hbChoice.empty': 'This month’s bundle has not been synced yet',
  'hbChoice.platform.steam': 'Steam',
  'hbChoice.platform.blizzard': 'Battle.net',
  'hbChoice.platform.epic': 'Epic',
  'hbChoice.platform.ubisoft': 'Ubisoft',
  'hbChoice.platform.gog': 'GOG',
  'hbChoice.platform.origin': 'Origin',
  'hbChoice.platform.ea': 'EA App',
  'hbChoice.platform.itch': 'itch.io',
  'hbChoice.platform.drm-free': 'DRM-free',
} as const

export default hbChoice
