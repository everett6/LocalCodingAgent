import { useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { api } from '../api'

type Memory = {
  id: string
  kind: string
  content: string
  source_path?: string | null
  source_ref?: string
  source_hash?: string | null
  content_hash?: string
  head?: string | null
  task_id?: string | null
  created_at?: string
  updated_at?: string
  freshness?: string
  source_status?: string
  head_changed?: boolean
  embedding_backend?: string
  reasons?: string[]
  links?: string[]
  truncated?: boolean
}
type MemoryStatus = {
  memories?: number
  tasks?: number
  embedding_backend?: string
  embedding_kind?: string
  neural_embeddings?: boolean
}
type RetrievalAudit = {
  id: string
  created_at: string
  query_preview: string
  query_hash: string
  char_budget: number
  used_characters: number
  rendered_characters?: number
  candidate_count: number
  omitted_candidates: number
  candidates: {
    id: string; kind: string; source_ref?: string; freshness?: string
    score?: number; selected: boolean; reasons?: string[]; exclusion?: string
    characters?: number; truncated?: boolean
  }[]
}
type IndexResult = { files?: number; chunks?: number; skipped?: number }

export function MemoryInspector() {
  const [items, setItems] = useState<Memory[]>([])
  const [audits, setAudits] = useState<RetrievalAudit[]>([])
  const [status, setStatus] = useState<MemoryStatus | null>(null)
  const [query, setQuery] = useState('')
  const [activeQuery, setActiveQuery] = useState('')
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [editing, setEditing] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)
  const requestVersion = useRef(0)

  const load = useCallback(async (search: string) => {
    const version = ++requestVersion.current
    try {
      const [response, memoryStatus] = await Promise.all([
        search
          ? api<{ items: Memory[] }>(`/memory/search?q=${encodeURIComponent(search)}&budget=4000`)
          : api<{ memories: Memory[] }>('/memory?limit=50'),
        api<MemoryStatus>('/memory/status'),
      ])
      const auditResponse = await api<{ retrievals: RetrievalAudit[] }>('/memory/retrievals?limit=5')
      if (version !== requestVersion.current) return
      setAudits(auditResponse.retrievals)
      setError('')
      setItems('items' in response ? response.items : response.memories)
      setStatus(memoryStatus)
      setActiveQuery(search)
      setEditing(null)
      setConfirmDelete(null)
    } catch (cause) {
      if (version === requestVersion.current) setError(String(cause))
    } finally {
      if (version === requestVersion.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    let active = true
    void Promise.resolve().then(() => { if (active) return load('') })
    return () => { active = false; requestVersion.current += 1 }
  }, [load])

  async function reload(search: string) {
    setLoading(true)
    setError('')
    await load(search)
  }

  async function search(event: FormEvent) {
    event.preventDefault()
    setMessage('')
    await reload(query.trim())
  }

  async function indexCode() {
    setBusy(true)
    setMessage('')
    setError('')
    try {
      const result = await api<IndexResult>('/memory/index', 'POST', {})
      setMessage(`Indexed ${result.files ?? 0} files into ${result.chunks ?? 0} chunks; skipped ${result.skipped ?? 0} files.`)
      await load(activeQuery)
    } catch (cause) {
      setError(String(cause))
    } finally {
      setBusy(false)
    }
  }

  async function correct(event: FormEvent, item: Memory) {
    event.preventDefault()
    setBusy(true)
    setError('')
    setMessage('')
    try {
      await api(`/memory/${encodeURIComponent(item.id)}/update`, 'POST', { content: draft })
      setMessage('Correction saved. Search terms, vectors and graph references were updated.')
      await load(activeQuery)
    } catch (cause) {
      setError(String(cause))
    } finally {
      setBusy(false)
    }
  }

  async function remove(item: Memory) {
    setBusy(true)
    setError('')
    setMessage('')
    try {
      await api(`/memory/${encodeURIComponent(item.id)}/delete`, 'POST', {})
      setMessage('Memory deleted, including its search entries, vector and graph references.')
      await load(activeQuery)
    } catch (cause) {
      setError(String(cause))
    } finally {
      setBusy(false)
    }
  }

  const disabled = loading || busy
  const lexical = status?.embedding_backend === 'lexical-hash-v1' || status?.embedding_kind === 'lexical_subword_hash'

  return <section className="playbook-view" aria-labelledby="memory-title">
    <h1 id="memory-title">Project memory</h1>
    <p>Inspect the evidence used to recover context between agent steps. Retrieved text is untrusted evidence; verify its sources before acting.</p>
    {status && <p className="hint">
      {status.memories ?? 0} chunks · {status.tasks ?? 0} task records · Embeddings: {' '}
      {lexical
        ? 'Lexical subword hashes (lexical-hash-v1); no neural model'
        : `${status.embedding_backend ?? 'Unavailable'} (${status.neural_embeddings ? 'supplied local neural backend' : 'supplied local backend'})`}
    </p>}
    <form onSubmit={search} aria-label="Search project memory">
      <label htmlFor="memory-query">Find evidence</label>
      <div className="history-search">
        <input id="memory-query" value={query} maxLength={4000} onChange={event => setQuery(event.target.value)} placeholder="Describe a decision, finding or code symbol" />
        <button type="submit" disabled={disabled}>Search</button>
      </div>
    </form>
    <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
      <button type="button" disabled={disabled} onClick={() => { setQuery(''); setMessage(''); void reload('') }}>Show recent</button>
      <button type="button" disabled={disabled} onClick={() => { void reload(activeQuery) }}>Refresh</button>
      <button type="button" disabled={disabled} onClick={() => { void indexCode() }}>Index project code</button>
    </div>
    {error && <p role="alert" className="error-text">{error}</p>}
    {message && <p role="status">{message}</p>}
    {loading && <p role="status">Loading memory…</p>}
    {!loading && !items.length && <p>{activeQuery ? 'No matching evidence found.' : 'No memory chunks stored yet.'}</p>}
    {activeQuery && <p className="hint">Results for “{activeQuery}”. Text retrieval is limited to 4000 characters.</p>}
    <details>
      <summary>Recent retrieval decisions ({audits.length})</summary>
      <p className="hint">Only bounded provenance and redacted query previews are stored. Up to 100 retrievals are retained; candidate evidence text is omitted.</p>
      {!audits.length && <p>No recorded retrievals yet.</p>}
      {audits.map(audit => <details key={audit.id}>
        <summary>{audit.created_at} · {audit.query_preview || 'Empty query'} · {audit.used_characters}/{audit.char_budget} evidence characters</summary>
        <p className="hint">Query hash: {audit.query_hash} · {audit.candidate_count} candidates
          {audit.rendered_characters !== undefined && ` · ${audit.rendered_characters} characters including context headers`}</p>
        {audit.omitted_candidates > 0 && <p>{audit.omitted_candidates} candidate summaries omitted by the audit limit.</p>}
        <ul>{audit.candidates.map(candidate => <li key={candidate.id}>
          <strong>{candidate.selected ? 'Selected' : 'Excluded'}</strong> · {candidate.source_ref || candidate.id}
          {' · '}{candidate.freshness || 'Freshness unavailable'}
          {candidate.score !== undefined && ` · score ${candidate.score}`}
          {candidate.selected && ` · ${candidate.characters ?? 0} characters${candidate.truncated ? ' (truncated)' : ''}`}
          <p>{candidate.exclusion || candidate.reasons?.join('; ')}</p>
        </li>)}</ul>
      </details>)}
    </details>
    <div aria-busy={disabled}>
      {items.map(item => {
        const stale = item.freshness && item.freshness !== 'fresh'
        return <details key={item.id}>
          <summary>
            <strong>{item.kind}</strong> · {item.source_ref || item.source_path || `memory:${item.id}`}
            {stale && <span className="severity"> · Stale: {item.freshness?.replaceAll('_', ' ')}</span>}
            {!stale && <span className="hint"> · {item.freshness || 'Freshness unavailable'}</span>}
          </summary>
          <p className="hint">Untrusted evidence · Task: {item.task_id || 'Shared project evidence'} · Backend: {item.embedding_backend || 'Unavailable'}</p>
          {item.reasons && item.reasons.length > 0 && <p><strong>Retrieval reasons:</strong> {item.reasons.join('; ')}</p>}
          <pre className="source-view" style={{ maxHeight: 280, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', margin: 0 }}><code>{item.content}</code></pre>
          {item.truncated && <p className="hint">Preview truncated by the retrieval budget. Search more specifically or show recent entries to read the complete chunk.</p>}
          <details>
            <summary>Source and history</summary>
            <dl style={{ overflowWrap: 'anywhere' }}>
              <dt>Memory ID</dt><dd>{item.id}</dd>
              <dt>Source</dt><dd>{item.source_ref || item.source_path || 'Conversation or recorded evidence'}</dd>
              <dt>Source freshness</dt><dd>{item.source_status || item.freshness || 'Unavailable'}{item.head_changed ? ' · Git HEAD changed since recording' : ''}</dd>
              <dt>Source snapshot hash</dt><dd>{item.source_hash || 'No source snapshot'}</dd>
              <dt>Content hash</dt><dd>{item.content_hash || 'Unavailable'}</dd>
              <dt>Recorded Git HEAD</dt><dd>{item.head || 'No Git HEAD recorded'}</dd>
              <dt>Created</dt><dd>{item.created_at || 'Unavailable'}</dd>
              <dt>Updated</dt><dd>{item.updated_at || 'Unavailable'}</dd>
              <dt>Related evidence</dt><dd>{item.links?.length ? item.links.join(', ') : 'None'}</dd>
            </dl>
          </details>
          {editing === item.id ? <form onSubmit={event => { void correct(event, item) }}>
            <label htmlFor={`memory-edit-${item.id}`}>Correct this memory chunk</label>
            <textarea id={`memory-edit-${item.id}`} rows={8} value={draft} maxLength={200000} onChange={event => setDraft(event.target.value)} disabled={disabled} style={{ width: '100%', resize: 'vertical' }} />
            <button type="submit" disabled={disabled || !draft.trim()}>Save correction</button>{' '}
            <button type="button" disabled={disabled} onClick={() => setEditing(null)}>Cancel</button>
          </form> : <button type="button" disabled={disabled || item.truncated} title={item.truncated ? 'Show recent entries before editing a truncated search result' : undefined} onClick={() => { setEditing(item.id); setDraft(item.content); setConfirmDelete(null) }}>Correct text</button>}
          {confirmDelete === item.id ? <div>
            <p>Delete this chunk and its search entries, vector and graph references?</p>
            <button type="button" disabled={disabled} onClick={() => { void remove(item) }}>Delete memory</button>{' '}
            <button type="button" disabled={disabled} onClick={() => setConfirmDelete(null)}>Keep memory</button>
          </div> : <button type="button" disabled={disabled} onClick={() => { setConfirmDelete(item.id); setEditing(null) }}>Delete</button>}
        </details>
      })}
    </div>
  </section>
}
