import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { api } from '../api'
import { ContextDebugger } from './ContextDebugger'

type Accounting = {
  availability: { installed: boolean }
  tokenizer: { configured: boolean; available: boolean | null; reason?: string; semantics: string }
  ledger: { requests: number; entries: { id: string; status: string; tokens_reserved: number; output_reserved: number; accounting: { prompt_reserved: number; tool_schema_bytes: number; component_tokens?: number }; provider_actual?: { total_tokens: number } }[] } | null
}

export function ContextAccountingPanel({ disabled = false }: { disabled?: boolean }) {
  const [status, setStatus] = useState<Accounting | null>(null), [error, setError] = useState('')
  const [path, setPath] = useState(''), [model, setModel] = useState(''), [digest, setDigest] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    const controller = new AbortController()
    void api<Accounting>('/context/accounting', 'GET', undefined, controller.signal).then(setStatus)
      .catch(cause => { if (!controller.signal.aborted) setError(String(cause)) })
    void api<{ provider?: { model_id: string } }>('/settings', 'GET', undefined, controller.signal)
      .then(result => setModel(result.provider?.model_id || '')).catch(() => {})
    return () => controller.abort()
  }, [])
  async function refresh() {
    setError('')
    try { setStatus(await api<Accounting>('/context/accounting')) } catch (cause) { setError(String(cause)) }
  }
  async function configure(event?: FormEvent) {
    event?.preventDefault(); setBusy(true); setError('')
    try { setStatus(await api<Accounting>('/context/tokenizer', 'POST', event ? { enabled: true, path, model_id: model, template_sha256: digest } : { enabled: false })) }
    catch (cause) { setError(String(cause)) }
    finally { setBusy(false) }
  }
  return <section className="context-accounting section-card" aria-label="Context accounting">
    <h2>Context accounting</h2>
    <p className="hint">Request reservations include messages, tool schemas, framing and the output allowance. Provider usage is shown separately when supplied.</p>
    <button type="button" onClick={() => { void refresh() }}>Refresh accounting</button>
    {status && <>
      <p>Local tokenizer: {status.tokenizer.available ? 'available' : status.tokenizer.configured ? 'configured · unavailable' : 'not configured'} · Full request: {status.tokenizer.semantics}</p>
      {status.tokenizer.reason && <p className="hint">{status.tokenizer.reason}</p>}
      <p className="hint">A matching local chat template can supply an exact component count. Provider formatting remains unverified, so admission retains a conservative bound.</p>
      {status.ledger ? <div className="workflow-table-scroll"><table className="workflow-table"><thead><tr><th>Request</th><th>Reserved</th><th>Output reserve</th><th>Tool schema bytes</th><th>Local template tokens</th><th>Provider actual</th></tr></thead><tbody>{status.ledger.entries.map(entry => <tr key={entry.id}><td>{entry.status}</td><td>{entry.tokens_reserved}</td><td>{entry.output_reserved}</td><td>{entry.accounting.tool_schema_bytes}</td><td>{entry.accounting.component_tokens ?? 'Unavailable'}</td><td>{entry.provider_actual?.total_tokens ?? 'Unavailable'}</td></tr>)}</tbody></table></div> : <p className="hint">Start a task to see its request accounting.</p>}
      <details><summary>Configure an existing local tokenizer</summary>
        {!status.availability.installed && <p className="hint">The optional Transformers tokenizer adapter is not installed. Conservative accounting remains active.</p>}
        <form onSubmit={event => { void configure(event) }} className="workflow-form">
          <label>Tokenizer folder inside this project<input value={path} onChange={event => setPath(event.target.value)} required /></label>
          <label>Matching model ID<input value={model} onChange={event => setModel(event.target.value)} required /></label>
          <label>Chat template SHA-256<input value={digest} onChange={event => setDigest(event.target.value)} pattern="[a-f0-9]{64}" required /></label>
          <button type="submit" disabled={disabled || busy || !status.availability.installed}>Use local tokenizer</button>
          <button type="button" disabled={disabled || busy || !status.tokenizer.configured} onClick={() => { void configure() }}>Disable tokenizer</button>
        </form>
      </details>
    </>}
    <ContextDebugger />
    {error && <p role="alert" className="error-text">{error}</p>}
  </section>
}
