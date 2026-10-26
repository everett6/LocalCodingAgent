import { useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { api } from '../api'

type Proof = { revision: string; test_path?: string; status?: string; [key: string]: unknown }
type Finding = {
  id: string; state: string; evidence_stale: boolean; suppression: string
  finding: { title: string; path: string; line: number; severity: string; rule: string; exploit_path: string; fix: string }
  review: (Proof & { verdict: string; input_trace: string; prerequisites: string; impact: string; counterevidence: string; reviewer_task: string }) | null
  before: Proof | null; after: Proof | null
}
type Job = { job_id: string; finding_id: string; action: string; status: string; result?: unknown; error?: string; quota_notice?: unknown; events?: unknown[] }
const active = (job: Job | null) => !!job && ['queued', 'running', 'cancelling'].includes(job.status)
const renderValue = (value: unknown) => typeof value === 'string' ? value : JSON.stringify(value, null, 2)

export function FindingsWorkbench() {
  const [findings, setFindings] = useState<Finding[]>([])
  const [selected, setSelected] = useState('')
  const [jobs, setJobs] = useState<Job[]>([])
  const [job, setJob] = useState<Job | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [reason, setReason] = useState('')
  const [phase, setPhase] = useState('before')
  const [testPath, setTestPath] = useState('tests/security_poc/')
  const [image, setImage] = useState('')
  const [backend, setBackend] = useState('docker')
  const [timeout, setTimeoutSeconds] = useState(60)
  const mounted = useRef(true)
  const load = useCallback(async () => {
    const response = await api<{ findings: Finding[]; jobs: Job[] }>('/findings')
    if (!mounted.current) return
    setFindings(response.findings)
    setJobs(response.jobs)
    setSelected(current => response.findings.some(row => row.id === current) ? current : response.findings[0]?.id ?? '')
    setJob(current => current ? response.jobs.find(row => row.job_id === current.job_id) ?? current : response.jobs.at(-1) ?? null)
  }, [])
  useEffect(() => {
    mounted.current = true
    void load().catch(cause => { if (mounted.current) setError(String(cause)) })
    return () => { mounted.current = false }
  }, [load])
  useEffect(() => {
    if (!active(job)) return
    let cancelled = false
    const timer = window.setInterval(() => {
      void api<Job>(`/findings/jobs/${job!.job_id}`).then(async value => {
        if (cancelled) return
        setJob(value)
        if (!active(value)) await load()
      }).catch(cause => { if (!cancelled) setError(String(cause)) })
    }, 1200)
    return () => { cancelled = true; window.clearInterval(timer) }
  }, [job, load])
  const finding = findings.find(row => row.id === selected)
  async function act(action: string, body: Record<string, unknown> = {}) {
    if (!finding) return
    setError(''); setBusy(true)
    try {
      const result = await api<Finding | Job>(`/findings/${finding.id}/${action}`, 'POST', body)
      if ('job_id' in result) setJob(result)
      await load()
    } catch (cause) { setError(String(cause)) }
    finally { setBusy(false) }
  }
  async function verify(event: FormEvent) {
    event.preventDefault()
    await act('verify', { phase, test_path: testPath, image, backend, timeout })
  }
  async function cancelJob() {
    if (!job) return
    try { setJob(await api<Job>(`/findings/jobs/${job.job_id}/cancel`, 'POST', {})) }
    catch (cause) { setError(String(cause)) }
  }
  const disabled = busy || active(job)
  return <section className="manager-view findings-workbench">
    <h2>Findings</h2>
    <p>Canonical source claims, independent review and revision-bound regression evidence. Review and preparation use the configured model and harness budgets.</p>
    {error && <p role="alert">{error}</p>}
    <button disabled={busy} onClick={() => void load().catch(cause => setError(String(cause)))}>Refresh findings</button>
    {findings.length === 0 && <p>No recorded findings. Run a local security review to discover candidates.</p>}
    <label>Finding <select value={selected} onChange={event => { setSelected(event.target.value); setReason('') }}>
      {findings.map(row => <option key={row.id} value={row.id}>{row.finding.title} · {row.state}{row.evidence_stale ? ' · stale' : ''}</option>)}
    </select></label>
    {finding && <article>
      <h3>{finding.finding.title}</h3>
      <p>{finding.finding.severity} · {finding.finding.path}:{finding.finding.line} · {finding.state}</p>
      <p>Canonical ID: <code>{finding.id}</code></p>
      {finding.evidence_stale && <p role="status">Evidence is stale: the current source revision differs from the recorded proof. Review current source before before-fix verification.</p>}
      <h4>Source evidence</h4>
      <dl><dt>Rule</dt><dd>{finding.finding.rule || 'Not recorded'}</dd>
        <dt>Exploit path</dt><dd>{finding.finding.exploit_path || 'Not recorded'}</dd>
        <dt>Proposed fix</dt><dd>{finding.finding.fix || 'Not recorded'}</dd></dl>
      <h4>Independent reviewer</h4>
      {finding.review ? <dl>{Object.entries(finding.review).map(([name, value]) => <div key={name}><dt>{name.replaceAll('_', ' ')}</dt><dd>{renderValue(value)}</dd></div>)}</dl> : <p>No independent review recorded.</p>}
      <button disabled={disabled || !!finding.suppression} onClick={() => void act('review')}>Run independent review</button>
      <button disabled={disabled || !!finding.suppression} onClick={() => void act('prepare')}>Prepare offline regression</button>
      <p>Preparation writes a safe-behavior test; verification records host-derived results from an already installed container image.</p>
      <form onSubmit={event => void verify(event)}>
        <label>Phase <select value={phase} onChange={event => setPhase(event.target.value)}><option value="before">Before fix</option><option value="after">After fix</option></select></label>
        <label>Test path <input required value={testPath} onChange={event => setTestPath(event.target.value)} /></label>
        <label>Installed trusted image <input required maxLength={512} value={image} onChange={event => setImage(event.target.value)} /></label>
        <label>Container backend <select value={backend} onChange={event => setBackend(event.target.value)}><option>docker</option><option>podman</option></select></label>
        <label>Timeout (seconds) <input type="number" required min={1} max={600} value={timeout} onChange={event => setTimeoutSeconds(Number(event.target.value))} /></label>
        <button disabled={disabled || !!finding.suppression || finding.review?.verdict !== 'supported'}>Verify regression</button>
      </form>
      {(['before', 'after'] as const).map(key => <section key={key}><h4>{key === 'before' ? 'Before-fix test evidence' : 'After-fix test evidence'}</h4>
        {finding[key] ? <pre>{JSON.stringify(finding[key], null, 2)}</pre> : <p>No accepted evidence.</p>}</section>)}
      {finding.suppression && <p>Suppression reason: {finding.suppression}</p>}
      <form onSubmit={event => { event.preventDefault(); void act('suppress', { reason }) }}>
        <label>Suppression reason <input required maxLength={2000} value={reason} onChange={event => setReason(event.target.value)} /></label>
        <button disabled={disabled || !reason.trim()}>Suppress finding</button>
      </form>
      <button disabled={disabled} onClick={() => void act('reopen')}>Reopen and clear accepted evidence</button>
    </article>}
    <h3>Finding jobs</h3>
    <label>Selected job <select value={job?.job_id ?? ''} onChange={event => setJob(jobs.find(row => row.job_id === event.target.value) ?? null)}>
      <option value="">Select a job</option>{jobs.map(row => <option key={row.job_id} value={row.job_id}>{row.action} · {row.status} · {row.finding_id}</option>)}
    </select></label>
    {job && <section aria-live="polite"><p>{job.action} · {job.status} · <code>{job.job_id}</code></p>
      <button disabled={!active(job) || job.status === 'cancelling'} onClick={() => void cancelJob()}>Cancel selected job</button>
      {job.error && <p role="alert">{job.error}</p>}
      {job.result != null && <pre>{renderValue(job.result)}</pre>}
      {job.quota_notice != null && <pre>{renderValue(job.quota_notice)}</pre>}
      <details><summary>Job events</summary><pre>{JSON.stringify(job.events ?? [], null, 2)}</pre></details>
    </section>}
  </section>
}
