import { useEffect, useState } from 'react'
import { api } from '../api'

type Check = { state: string; message: string; tool?: string; requires_approval?: boolean }
type Doctor = { servers: Record<string, Check>; message: string }
type Provenance = { retrieved_at: string; provider: string; tool: string; tool_version: string | null; document_version: string | null; document_versions?: (string | null)[]; citation_urls: string[] }

export function SearchConnectionDoctor() {
  const [doctor, setDoctor] = useState<Doctor | null>(null), [records, setRecords] = useState<Provenance[] | null>(null)
  const [busy, setBusy] = useState(''), [error, setError] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    void api<Doctor>('/integrations/search/doctor', 'GET', undefined, controller.signal).then(setDoctor)
      .catch(cause => { if (!controller.signal.aborted) setError(String(cause)) })
    return () => controller.abort()
  }, [])
  async function checkConnection(server: string) {
    setBusy(server); setError('')
    try {
      const result = await api<Doctor>('/integrations/search/doctor', 'POST', { server })
      setDoctor(previous => ({ ...result, servers: { ...previous?.servers, ...result.servers } }))
    } catch (cause) { setError(String(cause)) }
    finally { setBusy('') }
  }
  async function loadHistory() {
    setError('')
    try {
      const result = await api<{ records: { kind: string; data: { provenance?: Provenance } }[] }>('/research/evidence')
      setRecords(result.records.filter(record => record.kind === 'research_search' && record.data.provenance).map(record => record.data.provenance!))
    } catch (cause) { setError(String(cause)) }
  }
  return <section className="section-card" aria-label="Search connection doctor">
    <h2>Search connection doctor</h2>
    <p className="hint">Configure search servers in Connections. These checks never send a search query or enable a service. Checking a connection contacts the selected configured server to list its tools.</p>
    {doctor && <><p>{doctor.message}</p>{Object.entries(doctor.servers).map(([name, check]) => <div key={name}>
      <strong>{name} · {check.state.replaceAll('_', ' ')}</strong><p>{check.message}</p>
      {check.tool && <p className="hint">Tool: {check.tool} · {check.requires_approval ? 'Human approval required for searches' : 'Configured read-only tool; existing policies apply'}</p>}
      <button type="button" disabled={!!busy || ['disabled', 'missing_credentials'].includes(check.state)} onClick={() => { void checkConnection(name) }}>{busy === name ? 'Checking…' : 'Check search connection'}</button>
    </div>)}</>}
    <button type="button" onClick={() => { void loadHistory() }}>Inspect saved search provenance</button>
    {records && <><p className="hint">Latest evidence window. Retrieval records identify returned citations; they do not verify source claims or document freshness.</p>{records.length ? records.map((record, index) => <article key={index}>
      <p>{record.retrieved_at} · {record.provider} / {record.tool} · Tool version: {record.tool_version || 'Not supplied'} · Document version: {record.document_version || 'Not supplied'}</p>
      <ul>{record.citation_urls.filter(url => /^https?:\/\//i.test(url)).map((url, index) => <li key={url}><a href={url} target="_blank" rel="noopener noreferrer">{url}</a> · Document version: {record.document_versions?.[index] || 'Not supplied'}</li>)}</ul>
      {!record.citation_urls.length && <p>No structured citation URLs were supplied.</p>}
    </article>) : <p>No search provenance is present in the latest evidence window.</p>}</>}
    {error && <p role="alert" className="error-text">{error}</p>}
  </section>
}
