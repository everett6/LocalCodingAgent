import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'

type Result = {
  variant: string; status: string; settings: Record<string, unknown>
  ttft_ms?: number; tokens_per_second?: number | null; improved?: boolean; reason?: string
}
type Measurement = {
  id: string; created_at: string; status: string; restored: boolean; can_apply?: boolean
  measurement_trust?: string; error?: string; results: Result[]
}
type Job = { job_id: string; status: string; result?: Measurement; error?: string }
const active = (status?: string) => ['queued', 'running', 'cancelling'].includes(status ?? '')

export function InferenceTuningPanel() {
  const [history, setHistory] = useState<Measurement[]>([])
  const [job, setJob] = useState<Job | null>(null)
  const [variants, setVariants] = useState(['context', 'gpu', 'cache', 'draft'])
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const mounted = useRef(false)
  const request = useRef(0)
  const refresh = useCallback(async () => {
    const version = ++request.current
    const result = await api<{ history: Measurement[] }>('/inference/tuning/history')
    if (mounted.current && version === request.current) setHistory(result.history)
  }, [])
  useEffect(() => {
    mounted.current = true
    void refresh().catch(cause => { if (mounted.current) setError(String(cause)) })
    return () => { mounted.current = false; request.current += 1 }
  }, [refresh])
  const jobId = job?.job_id
  const running = active(job?.status)
  useEffect(() => {
    if (!jobId || !running) return
    let alive = true
    let pending = false
    const timer = setInterval(() => {
      if (pending) return
      pending = true
      void api<Job>(`/inference/tuning/jobs/${jobId}`).then(async result => {
        if (!alive || !mounted.current) return
        setJob(result)
        if (!active(result.status)) await refresh()
      }).catch(cause => { if (alive && mounted.current) setError(String(cause)) }).finally(() => { pending = false })
    }, 1000)
    return () => { alive = false; clearInterval(timer) }
  }, [jobId, running, refresh])

  async function start() {
    setBusy(true); setError(''); setMessage('')
    try {
      const result = await api<Job>('/inference/tuning/start', 'POST', { variants, repeats: 2 })
      if (mounted.current) setJob(result)
    } catch (cause) { if (mounted.current) setError(String(cause)) }
    finally { if (mounted.current) setBusy(false) }
  }
  async function cancel() {
    if (!job) return
    setBusy(true)
    try {
      const result = await api<Job>(`/inference/tuning/jobs/${job.job_id}/cancel`, 'POST', {})
      if (mounted.current) setJob(result)
    } catch (cause) { if (mounted.current) setError(String(cause)) }
    finally { if (mounted.current) setBusy(false) }
  }
  async function apply(record: Measurement, result: Result) {
    setBusy(true); setError(''); setMessage('')
    try {
      await api('/inference/tuning/apply', 'POST', { record_id: record.id, variant: result.variant })
      if (mounted.current) setMessage(`Applied measured ${result.variant} settings. Refresh local runtime settings to inspect the new configuration.`)
      await refresh()
    } catch (cause) { if (mounted.current) setError(String(cause)) }
    finally { if (mounted.current) setBusy(false) }
  }
  const records = job?.result && !history.some(record => record.id === job.result?.id) ? [job.result, ...history] : history
  return <section className="section-card" aria-labelledby="inference-tuning-title">
    <h2 id="inference-tuning-title">Measure local inference settings</h2>
    <p>Compare the configured local model and runtime with bounded setting changes. Each run restarts inference, performs one warmup and two measured requests per setting, then restores your original settings and running state.</p>
    <p className="hint">Up to five configurations, 32 output tokens per request, 20 seconds per request and 30 seconds per startup. Output tokens per second includes prefill and request overhead. Memory and VRAM telemetry is unavailable. Smaller context reduces the available token window.</p>
    <fieldset disabled={busy || running}>
      <legend>Compare with the current settings</legend>
      {['context', 'gpu', 'cache', 'draft'].map(variant => <label key={variant} style={{ marginRight: 16 }}>
        <input type="checkbox" checked={variants.includes(variant)} onChange={event => setVariants(previous => event.target.checked ? [...previous, variant] : previous.filter(value => value !== variant))} />{' '}
        {{ context: 'Half context', gpu: 'CPU / 16 GPU layers', cache: 'Toggle prompt cache', draft: 'Configured compatible draft / target only' }[variant]}
      </label>)}
    </fieldset>
    <button type="button" disabled={busy || running} onClick={() => { void start() }}>Start local benchmark</button>{' '}
    {running && <button type="button" disabled={busy || job?.status === 'cancelling'} onClick={() => { void cancel() }}>Cancel and restore</button>}
    {job && <p role="status">Benchmark: {job.status}{job.error ? ` · ${job.error}` : ''}</p>}
    {error && <p role="alert" className="error-text">{error}</p>}
    {message && <p role="status">{message}</p>}
    {records.map(record => <details key={record.id} open={record.id === job?.result?.id}>
      <summary>{record.created_at} · {record.status} · Baseline {record.restored ? 'restored' : 'restoration pending or failed'}</summary>
      {record.error && <p role="alert">{record.error}</p>}
      <p className="hint">Apply requires at least 5% higher measured output throughput and TTFT within 10% of baseline. Model, runtime and baseline settings must still match. Historical results from an earlier engine session must be measured again.</p>
      <table><thead><tr><th>Setting</th><th>Status</th><th>TTFT</th><th>Output tokens/s</th><th>Result</th></tr></thead>
        <tbody>{record.results.map(result => <tr key={result.variant}>
          <td>{result.variant}</td><td>{result.status}</td>
          <td>{result.ttft_ms === undefined ? 'Unknown' : `${result.ttft_ms.toFixed(1)} ms`}</td>
          <td>{result.tokens_per_second == null ? 'Unknown' : result.tokens_per_second.toFixed(2)}</td>
          <td>{result.reason || 'Measured baseline'}{record.can_apply && result.improved && <button type="button" disabled={busy || running} onClick={() => { void apply(record, result) }}>Apply measured settings</button>}</td>
        </tr>)}</tbody>
      </table>
      <details><summary>Measured configuration details</summary><pre>{JSON.stringify(record.results.map(result => ({ variant: result.variant, settings: result.settings })), null, 2)}</pre></details>
    </details>)}
  </section>
}
