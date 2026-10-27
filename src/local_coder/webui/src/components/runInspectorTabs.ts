export const INSPECTOR_TABS = ['overview', 'activity', 'artifacts'] as const
export type InspectorTab = typeof INSPECTOR_TABS[number]

/** Follow the WAI-ARIA horizontal tab pattern without intercepting Tab itself. */
export function inspectorTabForKey(current: InspectorTab, key: string): InspectorTab | null {
  if (key === 'Home') return INSPECTOR_TABS[0]
  if (key === 'End') return INSPECTOR_TABS[INSPECTOR_TABS.length - 1]
  if (key !== 'ArrowLeft' && key !== 'ArrowRight') return null
  const direction = key === 'ArrowRight' ? 1 : -1
  const index = INSPECTOR_TABS.indexOf(current)
  return INSPECTOR_TABS[(index + direction + INSPECTOR_TABS.length) % INSPECTOR_TABS.length]
}
