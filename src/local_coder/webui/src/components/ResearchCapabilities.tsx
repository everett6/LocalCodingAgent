import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'

export type ResearchCapabilityStatus = 'implemented' | 'partial' | 'external_required' | 'planned' | 'synthetic_only'
export interface ResearchCapability {
  id: number
  title: string
  requested_behavior: string
  group_id: number
  group: string
  status: ResearchCapabilityStatus
  summary: string
  evidence: { path: string; kind: 'source' | 'test'; note: string }[]
  limitations: string[]
}
export interface ResearchCapabilityCatalog {
  total: number
  capabilities: ResearchCapability[]
  groups: { id: number; title: string; count: number }[]
  statuses: { id: ResearchCapabilityStatus; title: string }[]
  counts: Record<ResearchCapabilityStatus, number>
  scope: string
  status_note: string
}

/** Read-only inventory: opening this panel never runs a research tool. */
export function ResearchCapabilities() {
  const [catalog, setCatalog] = useState<ResearchCapabilityCatalog | null>(null)
  const [query, setQuery] = useState('')
  const [group, setGroup] = useState('all')
  const [status, setStatus] = useState('all')
  const [error, setError] = useState('')
  const [reload, setReload] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    void api<ResearchCapabilityCatalog>('/research/capabilities', 'GET', undefined, controller.signal)
      .then(value => { if (!controller.signal.aborted) { setCatalog(value); setError('') } })
      .catch(cause => { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : 'Cannot load research capabilities') })
    return () => controller.abort()
  }, [reload])
  const items = useMemo(() => {
    const words = query.toLocaleLowerCase().trim().split(/\s+/).filter(Boolean)
    return (catalog?.capabilities || []).filter(item =>
      (group === 'all' || String(item.group_id) === group) &&
      (status === 'all' || item.status === status) &&
      words.every(word => `${item.id} ${item.title} ${item.group} ${item.summary} ${item.requested_behavior}`.toLocaleLowerCase().includes(word)))
  }, [catalog, query, group, status])
  const label = (value: ResearchCapabilityStatus) => catalog?.statuses.find(item => item.id === value)?.title || value.replaceAll('_', ' ')
  return <section className="research-capabilities" aria-labelledby="research-capabilities-title">
    <header><h1 id="research-capabilities-title">Research capabilities</h1><p>Every requested upgrade, with its current coverage and evidence.</p></header>
    {error && <div role="alert"><p>{error}</p><button type="button" onClick={() => setReload(value => value + 1)}>Retry loading</button></div>}
    {!catalog && !error && <p role="status">Loading capability inventory…</p>}
    {catalog && <>
      <p className="hint">{catalog.status_note}</p>
      <div className="research-status-counts" aria-label="Capability coverage">
        <button type="button" aria-pressed={status === 'all'} onClick={() => setStatus('all')}>All {catalog.total}</button>
        {catalog.statuses.map(item => <button key={item.id} type="button" aria-pressed={status === item.id} onClick={() => setStatus(item.id)}>{item.title} <strong>{catalog.counts[item.id]}</strong></button>)}
      </div>
      <div className="research-filters">
        <label>Search capabilities<input type="search" placeholder="Name, number, keyword…" value={query} onChange={event => setQuery(event.target.value)} /></label>
        <label>Area<select value={group} onChange={event => setGroup(event.target.value)}><option value="all">All eight areas</option>{catalog.groups.map(item => <option key={item.id} value={String(item.id)}>{item.id}. {item.title} ({item.count})</option>)}</select></label>
        <label>Coverage<select value={status} onChange={event => setStatus(event.target.value)}><option value="all">All statuses</option>{catalog.statuses.map(item => <option key={item.id} value={item.id}>{item.title}</option>)}</select></label>
      </div>
      <p role="status" aria-live="polite">Showing {items.length} of {catalog.total} capabilities</p>
      {items.length === 0 && <p>No capabilities match these filters. <button type="button" onClick={() => { setQuery(''); setGroup('all'); setStatus('all') }}>Clear filters</button></p>}
      <div className="research-capability-list">{items.map(item => <details className={`research-capability ${item.status}`} key={item.id}>
        <summary><span className="research-capability-number">{String(item.id).padStart(3, '0')}</span><strong>{item.title}</strong><span className={`research-capability-status ${item.status}`}>{label(item.status)}</span></summary>
        <div className="research-capability-body"><p className="hint">Area {item.group_id} · {item.group}</p><p>{item.summary}</p>
          <h3>Requested behavior</h3><p>{item.requested_behavior}</p>
          {item.limitations.length > 0 && <><h3>Current limits</h3><ul>{item.limitations.map(limit => <li key={limit}>{limit}</li>)}</ul></>}
          <h3>Evidence</h3>{item.evidence.length > 0 ? <ul>{item.evidence.map(evidence => <li key={`${evidence.kind}:${evidence.path}`}><span>{evidence.kind === 'source' ? 'Implementation' : 'Regression coverage'}: </span><code>{evidence.path}</code><p className="hint">{evidence.note}</p></li>)}</ul> : <p className="hint">No implementation evidence is registered for this requirement.</p>}
        </div>
      </details>)}</div>
      <p className="research-scope-note hint">{catalog.scope}</p>
    </>}
  </section>
}
