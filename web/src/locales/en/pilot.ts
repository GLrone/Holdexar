/* ════════════════════════════════════════════════════════════════════
   Pilot entries: console drawer (HlPilotDrawer.vue) and the settings
   Pilot card. Product nouns: Pilot (assistant) · Console (ask panel) ·
   Briefing (fact summary).
   ════════════════════════════════════════════════════════════════════ */

const pilot = {
  'pilot.title': 'Pilot',
  'pilot.console': 'Pilot Console',
  'pilot.briefing': 'Briefing',
  'pilot.ask.entry': 'Ask the Pilot',
  'pilot.ask.placeholder': 'Ask the Pilot: is this game worth buying now?',
  'pilot.ask.button': 'Ask',
  'pilot.context.of': 'Current game',
  'pilot.loading': 'Crunching the numbers…',
  'pilot.thinking': 'Thinking',
  'pilot.thinking.live': 'Thinking…',
  'pilot.cached': 'cached',
  'pilot.error': 'The Pilot Console could not finish this request. Please try again.',
  'pilot.guide.title': 'How to do it',
  'pilot.guide.monitor': 'Follow a game: open its detail page and press "Follow" to track prices.',
  'pilot.guide.alert': 'Price alert: set "Notify me below a price" in the alert area of the game detail page.',
  'pilot.guide.link': 'Browse the library',
  'pilot.reason.llm_off': 'AI narration is not configured yet — set the service URL and API key under "Settings → Pilot".',
  'pilot.reason.cap_reached': 'The monthly AI token budget is used up (adjustable in "Settings → Pilot"). Below is the fact summary from real data.',
  'pilot.reason.llm_failed': 'AI narration is temporarily unavailable. Below is the fact summary from real data.',
  'pilot.reason.no_data': 'No related price data in the library yet. Add the game in the library first, then ask again.',
  'pilot.facts.price': '{name} (CN region) now {price}{discount}, all-time low {lowest}. Past year: low {ymin} / high {ymax} ({count} observations).',
  'pilot.facts.price_noyear': '{name} (CN region) now {price}{discount}, all-time low {lowest}.',
  'pilot.facts.discount': ' (-{discount}%)',
  'pilot.facts.noLowest': 'no record yet',
  'pilot.facts.gamesTitle': 'Related games in the library:',
  'pilot.settings.title': 'Pilot',
  'pilot.settings.desc': 'AI narration source for the Pilot price assistant. Any OpenAI-compatible service works; the console shows fact summaries even without it.',
  'pilot.settings.enabled': 'Enable AI narration',
  'pilot.settings.base_url': 'Service URL (OpenAI-compatible, up to /v1)',
  'pilot.settings.model': 'Model name',
  'pilot.settings.api_key': 'API Key',
  'pilot.settings.api_key_hint': 'Stored encrypted; leave blank to keep the current one',
  'pilot.settings.monthly_cap': 'Monthly token cap',
  'pilot.settings.usage': '{tokens} tokens used this month',
  'pilot.settings.save': 'Save Pilot settings',
  'pilot.settings.saved': 'Saved',
} as const

export default pilot
