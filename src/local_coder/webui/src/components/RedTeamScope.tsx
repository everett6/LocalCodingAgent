import { useEffect, useState } from 'react'
import { api } from '../api'

import type { EngagementDraft } from '../redteam'

export function RedTeamScope({ draft, setDraft, selected, select, disabled }: { draft: EngagementDraft; setDraft: (draft: EngagementDraft) => void; selected: string; select: (id: string) => void; disabled: boolean }) {
  const [engagements, setEngagements] = useState<{ id: string; name: string }[]>([]), [error, setError] = useState('')
  useEffect(() => { let active = true; void api<{ engagements: { id: string; name: string }[] }>('/engagements').then(result => { if (active) { setEngagements(result.engagements); setError('') } }).catch(error => { if (active) setError(String(error)) }); return () => { active = false } }, [selected])
  const field = (name: keyof EngagementDraft, value: string | boolean | number) => setDraft({ ...draft, [name]: value })
  return <section className="redteam-scope" aria-label="Red team engagement"><h2>Red team engagement</h2><fieldset disabled={disabled}>
    <label>Engagement<select aria-label="Red team engagement" value={selected} onChange={event => select(event.target.value)}><option value="">Create a new engagement</option>{engagements.map(item => <option value={item.id} key={item.id}>{item.name} · {item.id}</option>)}</select></label>
    {!selected && <><label>Name<input value={draft.name} onChange={event => field('name', event.target.value)} placeholder="Application security exercise" maxLength={160} /></label>
    <div className="scope-columns"><label>In-scope local paths<textarea aria-label="Red team local paths" value={draft.paths} onChange={event => field('paths', event.target.value)} rows={2} required /></label><label>Excluded local paths<textarea value={draft.excluded_paths} onChange={event => field('excluded_paths', event.target.value)} rows={2} placeholder="One path per line" /></label></div>
    <label>Local HTTP origins (localhost/loopback only)<textarea aria-label="Local HTTP origins" value={draft.origins} onChange={event => field('origins', event.target.value)} rows={2} placeholder="http://127.0.0.1:8000 — one exact origin per line" /></label>
    <label className="scope-checkbox"><input type="checkbox" checked={draft.active_http} onChange={event => field('active_http', event.target.checked)} />Enable loopback-only local HTTP observations</label>
    <p className="scope-hint">Network checks are restricted to localhost and loopback IPs. Remote targets are not supported.</p>
    {draft.active_http && <><label>Authorization reference<input aria-label="Authorization reference" value={draft.authorization} onChange={event => field('authorization', event.target.value)} required placeholder="Local lab authorization or rules of engagement; no credentials" maxLength={2000} /></label><div className="scope-columns"><label>Excluded URLs<textarea value={draft.excluded_urls} onChange={event => field('excluded_urls', event.target.value)} rows={2} placeholder="Full URL prefixes, one per line" /></label><label>Request budget<input type="number" min={1} max={50} value={draft.max_requests} onChange={event => field('max_requests', Number(event.target.value))} /></label></div></>}
    <p>Scope is saved for this engagement. Optional HTTP observations stay on localhost/loopback and use GET, HEAD or OPTIONS without redirects or credentials. Source review remains available with HTTP checks off.</p></>}
    {selected && <p>The saved scope and remaining request budget apply to this task.</p>}{error && <p role="alert">{error}</p>}
  </fieldset></section>
}
export function EngagementEvidence({ id, status }: { id: string; status?: string }) {
  const [report, setReport] = useState('')
  useEffect(() => {
    let active = true
    const load = () => { void api<{ markdown: string }>(`/engagements/${encodeURIComponent(id)}/report`).then(result => { if (active) setReport(result.markdown) }).catch(error => { if (active) setReport(String(error)) }) }
    load(); const timer = setInterval(load, 4000)
    return () => { active = false; clearInterval(timer) }
  }, [id, status])
  return <details className="engagement-evidence"><summary>Engagement evidence & coverage · {id}</summary><pre className="report">{report || 'Loading evidence…'}</pre></details>
}
