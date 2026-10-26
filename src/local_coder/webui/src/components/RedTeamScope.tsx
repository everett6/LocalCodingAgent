import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { engagementPayload } from '../redteam'
import type { EngagementDraft } from '../redteam'

type SavedScope = Omit<EngagementDraft, 'paths' | 'excluded_paths' | 'origins' | 'excluded_urls'> & { objective: string; paths: string[]; excluded_paths: string[]; origins: string[]; excluded_urls: string[] }
type Defaults = { settings: SavedScope; env_path: string; env_text: string; preset: 'source-review' | 'api-review'; last_engagement_id: string; preview: { valid: boolean; message: string } }
const localAuthorization = 'The operator requested a local source-code security review of this project.'
const defaultObjective = 'Review the local project source for security weaknesses and report evidence-linked findings.'
const fromScope = (scope: SavedScope): EngagementDraft => ({ name: scope.name, paths: scope.paths.join('\n'), excluded_paths: scope.excluded_paths.join('\n'), origins: scope.origins.join('\n'), excluded_urls: scope.excluded_urls.join('\n'), authorization: scope.authorization, active_http: scope.active_http, max_requests: scope.max_requests })

function normalizedOrigin(value: string) {
  let input = value.trim()
  if (!input) return ''
  if (input === '::1') input = '[::1]'
  if (!input.includes('://')) input = `${/^(localhost(?=:|$)|127\.\d+\.\d+\.\d+(?=:|$)|\[::1\](?=:|$))/i.test(input) ? 'http' : 'https'}://${input}`
  try {
    if (!/^https?:\/\/[^/?#]+\/?$/i.test(input)) throw new Error('Use exact origins without paths.')
    const parsed = new URL(input)
    if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password || parsed.search || parsed.hash || !['', '/'].includes(parsed.pathname) || input.includes('\\')) throw new Error('Use exact origins without paths or credentials.')
    return parsed.origin
  } catch { throw new Error('Use a hostname or exact HTTP(S) origin, without paths or credentials.') }
}

export function RedTeamScope({ draft, setDraft, selected, select, disabled, onReviewProject }: { draft: EngagementDraft; setDraft: (draft: EngagementDraft) => void; selected: string; select: (id: string) => void; disabled: boolean; onReviewProject?: () => void }) {
  const [engagements, setEngagements] = useState<{ id: string; name: string }[]>([])
  const [defaults, setDefaults] = useState<Defaults | null>(null), [preset, setPreset] = useState<'source-review' | 'api-review'>('source-review')
  const [error, setError] = useState(''), [notice, setNotice] = useState(''), [saving, setSaving] = useState(false), [loading, setLoading] = useState(true)
  const dirty = useRef(false)
  useEffect(() => {
    let active = true
    void api<Defaults>('/engagements/defaults').then(result => {
      if (!active) return
      setDefaults(result); setPreset(result.preset)
      if (!dirty.current) setDraft(fromScope(result.settings))
    }).catch(error => { if (active) setError(error instanceof Error ? error.message : 'Cannot load saved setup') }).finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [setDraft])
  useEffect(() => {
    let active = true
    void api<{ engagements: { id: string; name: string }[] }>('/engagements').then(result => { if (active) setEngagements(result.engagements) }).catch(error => { if (active) setError(String(error)) })
    return () => { active = false }
  }, [selected])
  const field = (name: keyof EngagementDraft, value: string | boolean | number) => { dirty.current = true; setNotice(''); setDraft({ ...draft, [name]: value }) }
  const choosePreset = (value: 'source-review' | 'api-review') => {
    dirty.current = true; setNotice(''); setPreset(value)
    if (value === 'source-review') setDraft({ ...draft, origins: '', excluded_urls: '', active_http: false, authorization: localAuthorization })
  }
  const acceptDefaults = (result: Defaults) => { setDefaults(result); setPreset(result.preset); setDraft(fromScope(result.settings)); dirty.current = false; setError(''); setNotice('Setup saved. Scope validated.') }
  async function save() {
    setSaving(true); setError('')
    try {
      const origins = draft.origins.split('\n').filter(value => value.trim()).map(normalizedOrigin).join('\n')
      const settings = engagementPayload({ ...draft, origins }, defaults?.settings.objective || defaultObjective)
      acceptDefaults(await api<Defaults>('/engagements/defaults', 'POST', { settings, preset }))
    } catch (error) { setError(error instanceof Error ? error.message : 'Cannot save setup') }
    finally { setSaving(false) }
  }
  async function createEngagement() {
    setSaving(true); setError(''); setNotice('')
    try {
      const origins = draft.origins.split('\n').filter(value => value.trim()).map(normalizedOrigin).join('\n')
      const payload = engagementPayload({ ...draft, origins }, defaults?.settings.objective || defaultObjective)
      const result = await api<{ id: string }>('/engagements', 'POST', payload)
      select(result.id)
      setNotice('Scoped engagement created. No HTTP requests were made. Continue the review when you are ready.')
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Cannot create the scoped engagement') }
    finally { setSaving(false) }
  }
  async function importDefaults(file: File | undefined) {
    if (!file) return
    setSaving(true); setError('')
    try {
      if (file.size > 32000) throw new Error('Choose a redteam.defaults.env file smaller than 32 KB.')
      acceptDefaults(await api<Defaults>('/engagements/defaults', 'POST', { env_text: await file.text() }))
    } catch (error) { setError(error instanceof Error ? error.message : 'Cannot import setup') }
    finally { setSaving(false) }
  }
  function exportDefaults() {
    if (!defaults) return
    const url = URL.createObjectURL(new Blob([defaults.env_text], { type: 'text/plain' }))
    const link = document.createElement('a'); link.href = url; link.download = 'redteam.defaults.env'; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000)
  }
  async function copySelected() {
    setSaving(true); setError('')
    try {
      const result = await api<{ engagement: { scope: SavedScope } }>(`/engagements/${encodeURIComponent(selected)}/report`)
      setDraft(fromScope(result.engagement.scope)); setPreset(result.engagement.scope.origins.length ? 'api-review' : 'source-review'); select(''); dirty.current = true; setNotice('Saved scope copied. Review it before starting a new engagement.')
    } catch (error) { setError(error instanceof Error ? error.message : 'Cannot copy saved scope') }
    finally { setSaving(false) }
  }
  return <section className="redteam-scope section-card" aria-label="Red team engagement">
    <div className="redteam-quick-start"><div><h2>Review this project</h2><p>Start a source review, or create a scoped engagement for authorized HTTP checks.</p></div><span className="status-badge">{loading ? 'Loading setup' : selected ? 'Saved engagement' : draft.active_http ? 'HTTP setup · not active' : 'Source setup'}</span></div>
    <fieldset disabled={disabled || saving || loading}>
      <div className="redteam-summary"><span>Source scope</span><strong>{draft.paths.split('\n').filter(Boolean).join(', ') || 'Choose a project path'}</strong><span>{selected ? 'Inherited engagement scope applies' : draft.active_http ? 'HTTP setup awaits engagement creation' : 'HTTP checks off'}</span></div>
      <div className="redteam-actions"><button className="primary" type="button" disabled={!onReviewProject} onClick={onReviewProject}>{selected ? 'Continue this review' : 'Review this project'}</button>{!selected && <><button type="button" onClick={() => { void save() }}>{saving ? 'Saving…' : 'Save setup'}</button>{(preset === 'api-review' || draft.active_http) && <button type="button" disabled={!draft.origins.trim() || (draft.active_http && !draft.authorization.trim())} onClick={() => { void createEngagement() }}>Create scoped engagement</button>}</>}</div>
      <details className="redteam-advanced"><summary>Scope and optional HTTP checks</summary>
        <label>Saved engagement<select aria-label="Red team engagement" value={selected} onChange={event => select(event.target.value)}><option value="">Use this project setup</option>{engagements.map(item => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label>
        {selected ? <div className="redteam-saved"><p>The selected engagement keeps its saved scope, evidence and remaining request budget.</p><button type="button" onClick={() => { void copySelected() }}>Copy scope into a new setup</button><EngagementEvidence id={selected} /></div> : <>
          <div className="redteam-presets" aria-label="Review preset"><button type="button" aria-pressed={preset === 'source-review'} className={preset === 'source-review' ? 'selected' : ''} onClick={() => choosePreset('source-review')}>Source review</button><button type="button" aria-pressed={preset === 'api-review'} className={preset === 'api-review' ? 'selected' : ''} onClick={() => choosePreset('api-review')}>API review</button></div>
          <label>Name<input value={draft.name} onChange={event => field('name', event.target.value)} placeholder="Review this project" maxLength={160} /></label>
          <div className="scope-columns form-grid"><label>Local source paths<textarea aria-label="Red team local paths" value={draft.paths} onChange={event => field('paths', event.target.value)} rows={2} required /></label><label>Excluded local paths<textarea value={draft.excluded_paths} onChange={event => field('excluded_paths', event.target.value)} rows={2} placeholder="One path per line" /></label></div>
          {(preset === 'api-review' || draft.origins || draft.active_http) && <><label>Allowed HTTP origins<textarea aria-label="Authorized HTTP origins" value={draft.origins} onChange={event => field('origins', event.target.value)} onBlur={() => { try { field('origins', draft.origins.split('\n').filter(value => value.trim()).map(normalizedOrigin).join('\n')); setError('') } catch (error) { setError(error instanceof Error ? error.message : 'Check allowed origins') } }} rows={2} placeholder="app.example.com or localhost:3000" /></label><p>Hostnames fill in HTTPS automatically. Loopback hosts use HTTP. Add only targets you are authorized to review.</p><label className="scope-checkbox"><input type="checkbox" checked={draft.active_http} onChange={event => { dirty.current = true; setNotice(''); setDraft({ ...draft, active_http: event.target.checked, authorization: event.target.checked && draft.authorization === localAuthorization ? '' : draft.authorization }) }} />Enable authorized HTTP observations</label>
            {draft.active_http && <><label>HTTP authorization reference<input aria-label="Authorization reference" value={draft.authorization} onChange={event => field('authorization', event.target.value)} required placeholder="Your explicit app authorization or rules of engagement" maxLength={2000} /></label><div className="scope-columns form-grid"><label>Excluded URLs<textarea value={draft.excluded_urls} onChange={event => field('excluded_urls', event.target.value)} rows={2} placeholder="Full URL prefixes, one per line" /></label><label>Request limit<input type="number" min={1} max={50} value={draft.max_requests} onChange={event => field('max_requests', Number(event.target.value))} /></label></div></>}
          </>}
          <p>Saving validates the scope without contacting any targets. HTTP observations require a separately selected, authorized engagement.</p>
        </>}
      </details>
      <details className="redteam-env"><summary>Saved preferences file</summary><p><code>{defaults?.env_path || '.local-coder/redteam.defaults.env'}</code></p><div className="redteam-actions"><button type="button" disabled={!defaults} onClick={exportDefaults}>Export saved setup</button><label>Import setup<input aria-label="Import red team preferences" type="file" accept=".env,text/plain" onChange={event => { void importDefaults(event.target.files?.[0]); event.target.value = '' }} /></label></div><p>Scope preferences only. The owner-only file contains no API keys.</p></details>
      {notice && <p role="status">{notice}</p>}{error && <p role="alert">{error}</p>}
    </fieldset>
  </section>
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
