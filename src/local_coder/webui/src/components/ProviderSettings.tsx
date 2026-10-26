import { useEffect, useRef, useState } from 'react'
import { X, KeyRound } from 'lucide-react'
import { api, presets } from '../api'
import type { Provider } from '../api'
import { LocalInferenceSettings } from './LocalInferenceSettings'
export function ProviderSettings({ provider, close, saved }: { provider: Provider | null; close: () => void; saved: (provider: Provider) => void }) {
  const [tab, setTab] = useState(provider?.name === 'on-device' ? 'local' : 'cloud')
  const dialog = useRef<HTMLDialogElement>(null)
  const [form, setForm] = useState<Provider>(provider || presets.glm)
  const [key, setKey] = useState('')
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  useEffect(() => { dialog.current?.showModal() }, [])
  async function save(event: React.FormEvent) {
    event.preventDefault(); setSaving(true); setError('')
    try { const result = await api<{ provider: Provider }>('/settings', 'POST', { ...form, api_key: key || undefined }); setKey(''); saved(result.provider); close() }
    catch (error) { setError(error instanceof Error ? error.message : 'Cannot save connection') }
    finally { setSaving(false) }
  }
  return <dialog ref={dialog} className="settings-dialog" onCancel={close} aria-labelledby="settings-title"><header><div><KeyRound size={18} /><h2 id="settings-title">Model & connection</h2></div><button className="icon-button" aria-label="Close settings" onClick={close}><X size={18} /></button></header><nav className="settings-tabs"><button className={tab === 'cloud' ? 'selected' : ''} onClick={() => setTab('cloud')}>Cloud provider</button><button className={tab === 'local' ? 'selected' : ''} onClick={() => setTab('local')}>On-device model</button></nav>{tab === 'local' ? <LocalInferenceSettings saved={saved} /> : <form onSubmit={save}>
    <label>Provider preset<select defaultValue="" onChange={event => { const preset = presets[event.target.value as keyof typeof presets]; if (preset) { setForm(preset); setKey('') } }}><option value="" disabled>Choose a preset</option>{Object.entries(presets).map(([id, item]) => <option key={id} value={id}>{item.label}</option>)}</select></label>
    <label>API protocol<select value={form.backend} onChange={event => setForm({ ...form, backend: event.target.value })}><option value="openai_compatible">OpenAI-compatible</option><option value="anthropic">Anthropic Messages</option><option value="ollama">Ollama</option></select></label>
    <label>Model ID<input required value={form.model_id} placeholder="Model ID available on your account" onChange={event => setForm({ ...form, model_id: event.target.value })} /></label>
    <label>API endpoint<input required type="url" value={form.base_url} onChange={event => setForm({ ...form, base_url: event.target.value })} /></label>
    <label>API key<input type="password" autoComplete="off" value={key} placeholder={provider?.key_set ? 'Key configured · leave blank to keep' : 'Paste your provider key'} onChange={event => setKey(event.target.value)} /></label>
    <label>Or use an environment variable<input value={form.api_key_env || ''} placeholder="PROVIDER_API_KEY" onChange={event => setForm({ ...form, api_key_env: event.target.value })} /></label>
    <div className="form-row"><label>Harness profile<select value={form.profile || 'generic'} onChange={event => setForm({ ...form, profile: event.target.value })}><option value="generic">Standard</option><option value="glm53">GLM 5.3 · preserved reasoning</option><option value="qwen38">Qwen 3.8 · streamed tools</option><option value="openrouter_deepseek">OpenRouter · DeepSeek</option><option value="openrouter_glm">OpenRouter · GLM</option><option value="openrouter_kimi">OpenRouter · Kimi</option></select></label><label>Output token budget<input type="number" min={128} max={32768} value={form.max_tokens || 4096} onChange={event => setForm({ ...form, max_tokens: Number(event.target.value) })} /></label></div>
    <label>Recovery strategy attempts<input type="number" min={1} max={10} value={form.strategy_attempt_limit || 3} onChange={event => setForm({ ...form, strategy_attempt_limit: Number(event.target.value) })} /></label>
    <p className="hint">Keys stay in this engine’s memory until it closes. Use environment variables for your own persistent setup. Model access, billing and endpoint region depend on your provider.</p>{error && <p role="alert" className="error-text">{error}</p>}<button className="primary" disabled={saving}>{saving ? 'Saving…' : 'Save connection'}</button>
  </form>}</dialog>
}
