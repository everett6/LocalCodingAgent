export type EngagementDraft = { name: string; paths: string; excluded_paths: string; origins: string; excluded_urls: string; authorization: string; active_http: boolean; max_requests: number }
export const emptyEngagement: EngagementDraft = { name: '', paths: '.', excluded_paths: '', origins: '', excluded_urls: '', authorization: '', active_http: false, max_requests: 20 }
export function engagementPayload(draft: EngagementDraft, objective: string) {
  const lines = (value: string) => value.split('\n').map(line => line.trim()).filter(Boolean)
  return { ...draft, name: draft.name.trim() || 'Red team exercise', objective, paths: lines(draft.paths), excluded_paths: lines(draft.excluded_paths), origins: lines(draft.origins), excluded_urls: lines(draft.excluded_urls) }
}
