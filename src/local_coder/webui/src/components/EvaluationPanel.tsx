import { useEffect, useState } from 'react'
import { api } from '../api'

type Report = {
  id: string; mode: string; suite_hash: string; created_at: number; limitation?: string
  quality: { cases: number; passed: number; false_positives: number; false_negatives: number; precision: number | null; recall: number | null; results: { case_id: string; kind: string; passed: boolean }[] }
  observed: { elapsed_seconds: number; model_calls: number; total_tokens: number | null; cost_usd: number | null }
  model?: { name: string; model_id: string; backend: string }; missing_metrics: string[]
}
type Catalog = { version: string; suite_hash: string; limitation: string; cases: { id: string; kind: string }[] }

export function EvaluationPanel() {
  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [history, setHistory] = useState<Report[]>([])
  const [report, setReport] = useState<Report | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [job, setJob] = useState<string | null>(null), [confirmed, setConfirmed] = useState(false)
  const [cap, setCap] = useState<number | null>(null)
  const [remediation, setRemediation] = useState<{ available: boolean; successful_fixes: number | null; reproduced_findings: number | null; stale_records: number | null } | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    void api<{ catalog: Catalog; history: Report[]; model_evaluation: { max_cost_usd: number | null }; project_remediation: typeof remediation }>('/evaluations', 'GET', undefined, controller.signal)
      .then(result => { setCatalog(result.catalog); setHistory(result.history); setCap(result.model_evaluation.max_cost_usd); setRemediation(result.project_remediation) })
      .catch(cause => { if (!controller.signal.aborted) setError(String(cause)) })
    return () => controller.abort()
  }, [])
  useEffect(() => {
    if (!job) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    async function poll() {
      try {
        const result = await api<{ status: string; result: Report | null; error: string }>(`/evaluations/jobs/${job}`, 'GET', undefined, controller.signal)
        if (controller.signal.aborted) return
        if (['queued', 'running', 'cancelling'].includes(result.status)) { timer = setTimeout(() => { void poll() }, 800); return }
        setJob(null); setBusy(false)
        if (result.result) { setReport(result.result); setHistory(items => [result.result!, ...items].slice(0, 30)) }
        if (result.status !== 'completed') setError(result.error || `Evaluation ${result.status}`)
      } catch (cause) { if (!controller.signal.aborted) { setError(String(cause)); setBusy(false); setJob(null) } }
    }
    void poll()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [job])
  async function runModel() {
    if (!catalog || !confirmed) return
    setBusy(true); setError('')
    try {
      const result = await api<{ job_id: string }>('/evaluations/model/run', 'POST', { suite_hash: catalog.suite_hash, confirm_model_calls: true, max_cases: 20, timeout_seconds: 60 })
      setJob(result.job_id)
    } catch (cause) { setError(String(cause)); setBusy(false) }
  }
  async function cancel() {
    if (!job) return
    try { await api(`/evaluations/jobs/${job}/cancel`, 'POST', {}) } catch (cause) { setError(String(cause)) }
  }
  async function run() {
    if (!catalog) return
    setBusy(true); setError('')
    try {
      const result = await api<Report>('/evaluations/run', 'POST', { suite_hash: catalog.suite_hash, max_cases: 20, timeout_seconds: 10 })
      setReport(result); setHistory(items => [result, ...items].slice(0, 30))
    } catch (cause) { setError(String(cause)) } finally { setBusy(false) }
  }
  const percent = (value: number | null) => value === null ? 'Unavailable' : `${(value * 100).toFixed(1)}%`
  return <section className="playbook-view" aria-labelledby="evaluation-title">
    <h1 id="evaluation-title">Evaluations</h1>
    {remediation && <p className="hint">{remediation.available ? `Recorded project evidence: ${remediation.successful_fixes} verified fixes · ${remediation.reproduced_findings} reproduced findings · ${remediation.stale_records} stale records. These counts are separate from synthetic suite scores.` : 'Recorded project remediation metrics are unavailable.'}</p>}
    <p>Run the fixed local Python suite against a small AST security detector. Source is parsed without executing it.</p>
    <p className="hint">{catalog?.limitation || 'Loading suite catalog…'} Model evaluation uses the configured security model and shared token, cost and provider quota controls. Opening this view makes no model calls.</p>
    {catalog && <p className="hint">{catalog.version} · {catalog.cases.length} vulnerable, fixed and harmless cases<br />Suite hash: <code style={{ overflowWrap: 'anywhere' }}>{catalog.suite_hash}</code></p>}
    <button disabled={busy || !catalog} onClick={() => { void run() }}>Run offline static evaluation</button>
    <p className="hint">Configured harness spending cap: {cap === null ? 'Not configured' : `$${cap.toFixed(4)}`}. A positive dollar cap is required for cloud, metered and unknown-cost models. Unknown pricing may prevent admission. Provider selection never changes automatically.</p>
    <label><input type="checkbox" checked={confirmed} disabled={busy} onChange={event => setConfirmed(event.target.checked)} /> I authorize fixed-suite calls to the configured security model under these spending controls.</label>
    <button disabled={busy || !catalog || !confirmed} onClick={() => { void runModel() }}>Run configured model evaluation</button>
    {job && <p role="status">Model evaluation running… <button onClick={() => { void cancel() }}>Cancel model evaluation</button></p>}
    {error && <p role="alert" className="error-text">{error}</p>}
    {report && <article aria-label="Evaluation report">
      <h2>{report.mode === 'offline_static' ? 'Static detector results' : 'Model results'}</h2>
      {report.model && <p>{report.model.name} · {report.model.model_id} · {report.model.backend}</p>}
      <p>{report.quality.passed}/{report.quality.cases} cases passed · False positives: {report.quality.false_positives} · False negatives: {report.quality.false_negatives}</p>
      <p>Precision: {percent(report.quality.precision)} · Recall: {percent(report.quality.recall)}</p>
      <p>Observed elapsed: {report.observed.elapsed_seconds.toFixed(4)}s · Model calls: {report.observed.model_calls} · Tokens: {report.observed.total_tokens ?? 'Unavailable'} · Cost: {report.observed.cost_usd === null ? 'Unavailable' : `$${report.observed.cost_usd.toFixed(6)}`}</p>
      {report.limitation && <p className="hint">{report.limitation}</p>}
      {!!report.missing_metrics.length && <p>Missing measurements: {report.missing_metrics.join(', ')}</p>}
      <ul>{report.quality.results.map(item => <li key={item.case_id}>{item.case_id} ({item.kind}): {item.passed ? 'Pass' : 'Fail'}</li>)}</ul>
    </article>}
    <h2>Recent runs</h2>
    {!history.length && <p>No recorded runs.</p>}
    <ul>{history.map(item => <li key={item.id}><button onClick={() => setReport(item)}>{new Date(item.created_at * 1000).toLocaleString()} · {item.mode === 'offline_static' ? 'Static detector' : `Model ${item.model?.name || ''}`} · {item.quality.passed}/{item.quality.cases}</button></li>)}</ul>
  </section>
}
