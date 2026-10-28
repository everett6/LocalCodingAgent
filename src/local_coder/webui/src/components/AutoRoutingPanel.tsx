import { useEffect, useState } from 'react'
import { api } from '../api'

type Settings = {
  auto_enabled: boolean
  economy_model: string | null
  local_classifier: string | null
  classifier_timeout_seconds: number
  allow_cross_provider: boolean
  use_loaded_local_classifier: boolean
}
type Routing = { settings: Settings; status: {
  economy_available: boolean; economy_model_id: string | null
  classifier_configured: boolean; classifier_ready: boolean; classifier_state: string; reason: string
} }

export function AutoRoutingPanel({ disabled = false }: { disabled?: boolean }) {
  const [data, setData] = useState<Routing | null>(null)
  const [draft, setDraft] = useState<Settings | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    void api<Routing>('/routing/settings', 'GET', undefined, controller.signal).then(result => {
      setData(result); setDraft(result.settings)
    }).catch(cause => { if (!controller.signal.aborted) setError(String(cause)) })
    return () => controller.abort()
  }, [])
  async function refresh(save = false) {
    setBusy(true); setError(''); setMessage('')
    try {
      const result = await api<Routing>('/routing/settings', save ? 'POST' : 'GET', save ? draft : undefined)
      setData(result); setDraft(result.settings)
      if (save) setMessage('Automatic routing settings saved.')
    } catch (cause) { setError(String(cause)) }
    finally { setBusy(false) }
  }
  function field<K extends keyof Settings>(key: K, value: Settings[K]) {
    setDraft(current => current ? { ...current, [key]: value } : null)
    setMessage('')
  }
  return <details open aria-label="Automatic routing">
    <summary>Automatic routing</summary>
    <p className="hint">Simple eligible work can use a cheaper model. Uncertain and security work keeps the selected model. Actual savings depend on usage; the classifier is optional.</p>
    {data && <>
      <p>{data.status.reason}</p>
      {data.status.economy_model_id && <p>Economy candidate: {data.status.economy_model_id}</p>}
      <p className="hint">Local classifier: {data.status.classifier_ready ? 'Ready' : data.status.classifier_configured ? 'Configured; readiness unverified' : 'Not ready or not configured'}.</p>
    </>}
    {draft && <form onSubmit={event => { event.preventDefault(); void refresh(true) }}>
      <fieldset disabled={disabled || busy}>
        <label className="scope-checkbox"><input type="checkbox" checked={draft.auto_enabled} onChange={event => field('auto_enabled', event.target.checked)} />Enable automatic routing</label>
        <label className="scope-checkbox"><input type="checkbox" checked={draft.use_loaded_local_classifier} onChange={event => field('use_loaded_local_classifier', event.target.checked)} />Use an already loaded on-device classifier for cloud tasks</label>
        <p className="hint">Qwen3-0.6B is a small classifier option; suitability depends on your hardware and runtime. Load a model separately in On-device setup. Saving never downloads or starts inference.</p>
        <label className="scope-checkbox"><input type="checkbox" checked={draft.allow_cross_provider} onChange={event => field('allow_cross_provider', event.target.checked)} />Allow routing to a configured model on another provider</label>
        <p className="hint">Cross-provider routing may send task content to that provider. Credentials stay with each configured provider.</p>
        <details><summary>Advanced configured models</summary>
          <label>Economy model configuration key<input value={draft.economy_model || ''} maxLength={100} pattern="[A-Za-z0-9_.-]*" onChange={event => field('economy_model', event.target.value || null)} placeholder="Automatic cached candidate" /></label>
          <label>Local classifier configuration key<input value={draft.local_classifier || ''} maxLength={100} pattern="[A-Za-z0-9_.-]*" onChange={event => field('local_classifier', event.target.value || null)} placeholder="Optional YAML model key" /></label>
          <label>Classifier timeout (seconds)<input type="number" min={0.25} max={5} step={0.25} value={draft.classifier_timeout_seconds} onChange={event => field('classifier_timeout_seconds', Number(event.target.value))} /></label>
          <p className="hint">Keys refer to models configured in YAML. Blank economy uses a verified cheaper cached candidate when available.</p>
        </details>
        <button type="submit" className="primary">{busy ? 'Saving…' : 'Save routing settings'}</button>
      </fieldset>
    </form>}
    <button type="button" disabled={busy} onClick={() => void refresh()}>Refresh routing status</button>
    {error && <p className="error-text" role="alert">{error}</p>}
    {message && <p role="status">{message}</p>}
  </details>
}
