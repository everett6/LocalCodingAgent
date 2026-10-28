import { useState } from 'react'
import { api } from '../api'
import { ResearchCapabilities } from './ResearchCapabilities'
import { DependencyReview } from './DependencyReview'
import { SearchConnectionDoctor } from './SearchConnectionDoctor'

type Finding = { path?: string; rule?: string; title?: string; file?: string; line?: number; rule_id?: string; message?: string; description?: string; severity?: string; cwe?: string; verification?: string }
type Audit = { findings: Finding[]; coverage?: { status: string; files_scanned: number; parse_errors: number; skipped: string[] }; limitations?: string[]; evidence_hash?: string }

function download(name: string, value: string, type: string) {
  const url = URL.createObjectURL(new Blob([value], { type }))
  const link = document.createElement('a'); link.href = url; link.download = name; link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export function ResearchWorkbench({ disabled = false }: { disabled?: boolean }) {
  const [view, setView] = useState('audit'), [target, setTarget] = useState('.')
  const [result, setResult] = useState<Audit | null>(null), [structure, setStructure] = useState('')
  const [integrity, setIntegrity] = useState(''), [busy, setBusy] = useState(false), [error, setError] = useState('')
  async function run() {
    setBusy(true); setError('')
    try { setResult(await api<Audit>('/research/audit', 'POST', { target_path: target, max_files: 200, max_findings: 100, max_bytes: 2000000 })) }
    catch (cause) { setError(cause instanceof Error ? cause.message : 'Source review failed') }
    finally { setBusy(false) }
  }
  async function exportReport(format: 'sarif' | 'markdown') {
    setError('')
    try {
      const report = await api<Record<string, unknown>>(`/research/report?format=${format}&path=${encodeURIComponent(target)}`)
      download(format === 'sarif' ? 'local-coder-findings.sarif' : 'local-coder-findings.md', format === 'sarif' ? JSON.stringify(report, null, 2) : String(report.markdown || ''), format === 'sarif' ? 'application/sarif+json' : 'text/markdown')
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Cannot export report') }
  }
  async function importReport(file?: File) {
    if (!file) return
    setBusy(true); setError('')
    try {
      if (file.size > 90000) throw new Error('Choose a SARIF report smaller than 90 KB.')
      setResult(await api<Audit>('/research/sarif/import', 'POST', { report: JSON.parse(await file.text()) }))
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Cannot import report') }
    finally { setBusy(false) }
  }
  async function inspectStructure() {
    setBusy(true); setError('')
    try { const result = await api<{ text: string }>(`/memory/structure?path=${encodeURIComponent(target)}&budget=12000`); setStructure(result.text) }
    catch (cause) { setError(cause instanceof Error ? cause.message : 'Cannot inspect indexed symbols') }
    finally { setBusy(false) }
  }
  async function verifyEvidence() {
    setError('')
    try { const result = await api<{ integrity: { valid: boolean; records: number; head_hash?: string } }>('/research/evidence'); setIntegrity(`${result.integrity.valid ? 'Hash chain intact' : 'Integrity check failed'} · ${result.integrity.records} records. Hashes check stored data; they do not prove exploitability. Without an independently retained head, full replacement or truncation cannot be detected.`) }
    catch (cause) { setError(cause instanceof Error ? cause.message : 'Cannot verify evidence') }
  }
  return <section className="research-workbench">
    <header><h1>Research workspace</h1><p>Review local code, inspect indexed symbols, and keep traceable findings.</p></header>
    <SearchConnectionDoctor />
    <nav className="research-tabs" aria-label="Research views"><button aria-pressed={view === 'audit'} onClick={() => setView('audit')}>Source review</button><button aria-pressed={view === 'dependencies'} onClick={() => setView('dependencies')}>Dependencies</button><button aria-pressed={view === 'capabilities'} onClick={() => setView('capabilities')}>Upgrade roadmap</button></nav>
    {view === 'capabilities' ? <ResearchCapabilities /> : view === 'dependencies' ? <DependencyReview disabled={disabled} /> : <>
      <div className="research-controls"><label>Project path<input aria-label="Research project path" value={target} onChange={event => setTarget(event.target.value)} /></label><button disabled={disabled || busy} onClick={() => { void run() }}>{busy ? 'Working…' : 'Review sources'}</button><button disabled={busy} onClick={() => { void inspectStructure() }}>Inspect indexed symbols</button></div>
      <p className="hint">Static analysis produces candidates for review. It does not execute project code or contact targets. Index source files in Project memory to populate the symbol view.</p>
      <div className="research-actions"><button onClick={() => { void exportReport('sarif') }}>Export SARIF</button><button onClick={() => { void exportReport('markdown') }}>Export Markdown</button><button onClick={() => { void verifyEvidence() }}>Check evidence integrity</button><label>Import SARIF<input type="file" aria-label="Import SARIF" accept=".sarif,.json,application/json" disabled={disabled || busy} onChange={event => { void importReport(event.target.files?.[0]); event.target.value = '' }} /></label></div>
      {integrity && <p role="status">{integrity}</p>}
      {result && <div className="research-results"><h2>{result.findings.length} unverified candidates</h2>{result.coverage && <aside role="status"><strong>{result.coverage.status} source coverage</strong><p className="hint">{result.coverage.files_scanned} Python files inspected · {result.coverage.parse_errors} parse errors</p>{result.coverage.skipped.map((reason, index) => <p className="hint" key={index}>{reason}</p>)}</aside>}{result.findings.map((finding, index) => <article key={index}><header><strong>{finding.rule_id || finding.rule || 'Imported finding'}</strong><span>{finding.severity} · {finding.cwe} · {finding.verification || 'unverified'}</span></header><p>{finding.message || finding.description || finding.title}</p><code>{finding.file || finding.path || 'Location not supplied'}{finding.line ? `:${finding.line}` : ''}</code></article>)}{result.evidence_hash && <p className="hint">Evidence hash: <code>{result.evidence_hash}</code></p>}{result.limitations?.map(note => <p className="hint" key={note}>{note}</p>)}</div>}
      {structure && <pre className="research-structure">{structure}</pre>}
    </>}
    {error && <p role="alert" className="error-text">{error}</p>}
  </section>
}
