import { useState } from 'react'
import { api } from '../api'

type Request = { id: string; task_id: string | null; composition: { instructions: number; schemas: number; history: number; retrieval: number; output_reserved: number }; attribution_method: string; retrieval_ids: string[]; usage: { prompt_tokens: number | null; completion_tokens: number | null } | null; cost_usd: number | null; outcome: string }
type Debugger = { requests: Request[]; retrievals: unknown[]; prompt_text_stored: boolean; retention_requests: number }

export function ContextDebugger() {
  const [task, setTask] = useState(''), [data, setData] = useState<Debugger | null>(null), [error, setError] = useState(''), [busy, setBusy] = useState(false)
  async function inspect() {
    setBusy(true); setError('')
    try { setData(await api<Debugger>(`/context/debugger?limit=20${task.trim() ? `&task_id=${encodeURIComponent(task.trim())}` : ''}`)) }
    catch (cause) { setError(String(cause)) }
    finally { setBusy(false) }
  }
  return <section aria-label="Context debugger">
    <h3>Context attribution and retrieval</h3>
    <p className="hint">Inspect bounded request components, retrieval omissions, provider usage and recorded outcomes. Component sizes are serialized UTF-8 bytes; output allowance is tokens. They do not establish exact provider tokenization.</p>
    <label>Optional task ID<input value={task} maxLength={100} onChange={event => setTask(event.target.value)} /></label>
    <button type="button" disabled={busy} onClick={() => { void inspect() }}>{busy ? 'Inspecting…' : 'Inspect context attribution'}</button>
    {data && <><p className="hint">Retention: {data.retention_requests} requests · Prompt text stored: {data.prompt_text_stored ? 'Yes' : 'No'}</p>
      {data.requests.length ? data.requests.map(request => <details key={request.id}><summary>{request.task_id || 'Unattributed request'} · {request.outcome}</summary>
        <dl>{Object.entries(request.composition).map(([name, count]) => <div key={name}><dt>{name.replaceAll('_', ' ')}</dt><dd>{count.toLocaleString()} {name === 'output_reserved' ? 'tokens reserved' : 'bytes'}</dd></div>)}</dl>
        <p>Provider usage: {request.usage ? `${request.usage.prompt_tokens ?? 'Unknown'} prompt / ${request.usage.completion_tokens ?? 'Unknown'} completion tokens` : 'Not supplied'} · Cost: {request.cost_usd === null ? 'Unknown' : `$${request.cost_usd.toFixed(6)}`}</p>
        <p>Retrieval IDs: {request.retrieval_ids.join(', ') || 'None recorded'}</p>
      </details>) : <p>No request attribution is available for this selection.</p>}
      <details><summary>Retrieval selection and omission evidence · {data.retrievals.length}</summary><pre>{JSON.stringify(data.retrievals, null, 2)}</pre></details>
    </>}
    {error && <p role="alert" className="error-text">{error}</p>}
  </section>
}
