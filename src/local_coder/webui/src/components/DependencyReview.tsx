import { useEffect, useState } from 'react'
import { api } from '../api'

type Component = { id: string; name: string; version: string | null; ecosystem: string; direct: boolean; conditional: boolean; provenance: { source: string } }
type Inventory = {
  components: Component[]; matches?: { id: string; package: string; severity?: string }[]
  unresolved: unknown[]; cache_status?: string; advisory_provenance?: { imported_at: string; source_label: string }
  coverage: { status: string; manifests_scanned: number; parse_errors: number; skipped: string[] }
}

export function DependencyReview({ disabled = false }: { disabled?: boolean }) {
  const [target, setTarget] = useState('.'), [report, setReport] = useState<Inventory | null>(null)
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [notice, setNotice] = useState('')
  const [advisoryId, setAdvisoryId] = useState(''), [source, setSource] = useState('osv-record')
  const [updates, setUpdates] = useState<{ total: number; withdrawn: number; provenance?: { imported_at: string; source_label: string } } | null>(null)
  async function loadUpdates() {
    try { setUpdates(await api('/research/advisories/updates')) } catch { /* Inspection remains available if updates are unavailable. */ }
  }
  useEffect(() => {
    const controller = new AbortController()
    void api<typeof updates>('/research/advisories/updates', 'GET', undefined, controller.signal)
      .then(setUpdates).catch(() => {})
    return () => controller.abort()
  }, [])
  async function refreshAdvisory() {
    setBusy(true); setError(''); setNotice('')
    try {
      const result = await api<{ imported: number; total: number }>('/research/advisories/update', 'POST', { source, advisory_id: advisoryId.trim() })
      setReport(null); setNotice(`Refreshed ${result.imported} package records; ${result.total} cached. Coverage remains incomplete. Inspect dependencies again.`)
      await loadUpdates()
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Cannot refresh official advisory record') }
    finally { setBusy(false) }
  }
  async function inspect() {
    setBusy(true); setError('')
    try { setReport(await api<Inventory>(`/research/dependencies?path=${encodeURIComponent(target)}&action=audit`)) }
    catch (cause) { setError(cause instanceof Error ? cause.message : 'Cannot inspect local dependencies') }
    finally { setBusy(false) }
  }
  async function importAdvisories(file?: File) {
    if (!file) return
    setBusy(true); setError(''); setNotice('')
    try {
      if (file.size > 90000) throw new Error('Choose an advisory dataset smaller than 90 KB.')
      const result = await api<{ imported: number; total: number }>('/research/advisories/import', 'POST', {
        dataset: JSON.parse(await file.text()), source_label: 'Operator file import',
      })
      await loadUpdates()
      setReport(null); setNotice(`Imported ${result.imported} records; ${result.total} cached. Run the inventory again to compare dependencies.`)
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Cannot import advisory dataset') }
    finally { setBusy(false) }
  }
  return <section className="dependency-review">
    <h2>Offline dependency review</h2>
    <p className="hint">Inspect npm lockfiles and Python declarations without installing packages. Reviewed version 2 records support bounded npm and Python advisory ranges. Unsupported ranges, markers and reachability require review. Missing matches do not establish safety.</p>
    <div className="research-controls"><label>Manifest or project path<input aria-label="Dependency project path" value={target} onChange={event => setTarget(event.target.value)} /></label><button disabled={disabled || busy} onClick={() => { void inspect() }}>{busy ? 'Working…' : 'Inspect dependencies'}</button><label>Import advisory dataset<input type="file" aria-label="Import advisory dataset" accept=".json,application/json" disabled={disabled || busy} onChange={event => { void importAdvisories(event.target.files?.[0]); event.target.value = '' }} /></label></div>
    <div className="research-controls"><label>Advisory source<select aria-label="Advisory source" value={source} onChange={event => setSource(event.target.value)} disabled={disabled || busy}><option value="osv-record">Official OSV individual record</option></select></label><label>Public advisory identifier<input aria-label="Public advisory identifier" placeholder="GHSA-xxxx-xxxx-xxxx, CVE or PYSEC" value={advisoryId} onChange={event => setAdvisoryId(event.target.value)} disabled={disabled || busy} /></label><button disabled={disabled || busy || !advisoryId.trim()} onClick={() => { void refreshAdvisory() }}>Refresh selected advisory</button></div>
    <p className="hint">Refresh downloads one public record from the official OSV source only after this button is pressed. No automatic updates. Cache coverage remains incomplete.</p>
    {updates && <p className="hint">{updates.total} cached package records · {updates.withdrawn} withdrawn. {updates.provenance ? `Last import: ${updates.provenance.imported_at} · ${updates.provenance.source_label}` : 'No advisory dataset imported.'}</p>}
    <details><summary>Advisory dataset format</summary><p className="hint">Import reviewed local data. The app does not update advisories automatically. Version 1 retains literal exact-version matching; use version 2 for evaluated affected_ranges or OSV records in the advisories array. Imported records remain unverified.</p><pre className="research-structure">{JSON.stringify({ version: 2, advisories: [{ id: 'CVE-2026-12345', ecosystem: 'pypi', package: 'example-package', versions: ['1.0.0'], summary: 'Synthetic format example' }] }, null, 2)}</pre></details>
    {notice && <p role="status">{notice}</p>}
    {report && <div className="research-results">
      <h3>{report.components.length} dependency records · {report.coverage.status} coverage</h3>
      <p className="hint">{report.coverage.manifests_scanned} manifests inspected · {report.coverage.parse_errors} parse errors · {report.unresolved.length} unresolved items · Advisory cache: {report.cache_status || 'unavailable'}</p>
      {report.advisory_provenance && <p className="hint">Advisory provenance: {report.advisory_provenance.source_label} · {report.advisory_provenance.imported_at}</p>}
      {report.coverage.skipped.map((reason, index) => <p className="hint" key={index}>{reason}</p>)}
      <h3>{report.matches?.length || 0} imported advisory matches · unverified</h3>
      {report.matches?.slice(0, 100).map((item, index) => <p key={index}><strong>{item.id}</strong> · {item.package} · {item.severity || 'Severity unspecified'}</p>)}
      <p className="hint">Showing up to 100 matches and 150 components. The JSON download includes the complete bounded result.</p>
      <button onClick={() => {
        const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' }))
        const link = document.createElement('a'); link.href = url; link.download = 'local-coder-dependencies.json'; link.click()
        setTimeout(() => URL.revokeObjectURL(url), 1000)
      }}>Download inventory JSON</button>
      {report.components.slice(0, 150).map(item => <article key={item.id}><header><strong>{item.name}</strong><span>{item.ecosystem} · {item.version || 'Version unresolved'} · {item.direct ? 'direct' : 'transitive'}{item.conditional ? ' · conditional' : ''}</span></header><code>{item.provenance.source}</code></article>)}
    </div>}
    {error && <p role="alert" className="error-text">{error}</p>}
  </section>
}
