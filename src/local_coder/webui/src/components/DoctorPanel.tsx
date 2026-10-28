import { useEffect, useState } from 'react'
import { api } from '../api'

type Check = { status: string; summary: string; sandbox?: Check; [key: string]: unknown }
type Report = { checks: Record<string, Check>; repairs: { action: string; label: string }[]; network_probes: boolean }
type RepairResult = { verified: boolean; after: Report }

export function DoctorPanel({ disabled = false, onSettings }: { disabled?: boolean; onSettings?: () => void }) {
  const [report, setReport] = useState<Report | null>(null)
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [notice, setNotice] = useState('')
  async function refresh() {
    setBusy(true); setError('')
    try { setReport(await api<Report>('/doctor')) }
    catch { setError('Could not load local diagnostics. Refresh to try again.') }
    finally { setBusy(false) }
  }
  useEffect(() => {
    const controller = new AbortController()
    void api<Report>('/doctor', 'GET', undefined, controller.signal).then(setReport)
      .catch(() => { if (!controller.signal.aborted) setError('Could not load local diagnostics.') })
    return () => controller.abort()
  }, [])
  async function runRepair(action: string) {
    setBusy(true); setError(''); setNotice('')
    try {
      const result = await api<RepairResult>('/doctor/repair', 'POST', { action })
      setReport(result.after)
      setNotice(result.verified ? 'Repair verified against the current local state.' : 'Repair did not pass verification. Review the updated checks.')
    } catch { setError('Repair could not be verified. Refresh diagnostics before trying again.') }
    finally { setBusy(false) }
  }
  return <section className="settings-panel" aria-label="Doctor">
    <h2>Doctor</h2>
    <p>Inspect local setup, runtime files, engine discovery, IDE sandbox permissions and project state. Provider credentials are shown only as presence checks.</p>
    <div className="runtime-actions">
      <button type="button" disabled={busy} onClick={() => { void refresh() }}>Refresh diagnostics</button>
      {onSettings && <button type="button" onClick={onSettings}>Open connection settings</button>}
    </div>
    {busy && <p role="status">Checking local state…</p>}
    {error && <p role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    {report && <>
      <p>No provider requests, downloads or computer scans run during inspection. Engine ports are reported from private discovery metadata.</p>
      <div className="doctor-checks">{Object.entries(report.checks).map(([name, check]) => <article key={name}>
        <h3>{name.replaceAll('_', ' ')} · {check.status}</h3>
        <p>{check.summary}</p>
        <dl>{Object.entries(check).filter(([key, value]) => !['status', 'summary', 'sandbox'].includes(key) && ['string', 'number', 'boolean'].includes(typeof value)).map(([key, value]) => <div key={key}><dt>{key.replaceAll('_', ' ')}</dt><dd>{String(value)}</dd></div>)}</dl>
        {check.sandbox && <p>Sandbox · {check.sandbox.status}: {check.sandbox.summary}</p>}
      </article>)}</div>
      <h3>Available repairs</h3>
      <p>Each action rechecks its target while the engine is idle and verifies the result.</p>
      {report.repairs.length ? report.repairs.map(item => <button type="button" key={item.action} disabled={disabled || busy} onClick={() => { void runRepair(item.action) }}>{item.label}</button>) : <p>No automatic repairs are offered for the current checks.</p>}
    </>}
  </section>
}
