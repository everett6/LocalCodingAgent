import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { X, KeyRound, Cloud, Cpu, Check, Server } from 'lucide-react'
import { api, presets } from '../api'
import type { CatalogModel, ContextPolicy, Mode, ModelCatalog, Provider, ProviderUsage, SavedProviderProfile } from '../api'
import { LocalInferenceSettings } from './LocalInferenceSettings'
import { ModelCatalogPicker } from './ModelCatalogPicker'

function isOpenRouter(endpoint: string): boolean {
  try { return new URL(endpoint).hostname === 'openrouter.ai' }
  catch { return false }
}
function isLocalEndpoint(endpoint: string): boolean {
  try {
    const url = new URL(endpoint)
    return ['http:', 'https:'].includes(url.protocol) && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname) && !url.username && !url.password
  } catch { return false }
}
function presetFor(provider: Provider): string {
  if (isOpenRouter(provider.base_url)) return 'openrouter'
  return Object.entries(presets).find(([, item]) => item.base_url === provider.base_url && item.backend === provider.backend)?.[0] || 'custom'
}

export function ProviderSettings({ provider, close, saved, mode = 'coding', usage, contextPolicy, preferFree = false, preferLocal = false }: {
  provider: Provider | null
  close: () => void
  saved: (provider: Provider) => void
  mode?: Mode
  usage?: ProviderUsage | null
  contextPolicy?: ContextPolicy | null
  preferFree?: boolean
  preferLocal?: boolean
}) {
  const [tab, setTab] = useState(preferLocal || provider?.name === 'on-device' ? 'local' : 'cloud')
  const dialog = useRef<HTMLDialogElement>(null)
  const [form, setForm] = useState<Provider>(provider || presets.openrouter)
  const [key, setKey] = useState('')
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const [catalog, setCatalog] = useState<ModelCatalog | null>(null)
  const [catalogError, setCatalogError] = useState('')
  const [catalogLoading, setCatalogLoading] = useState(false)
  const [refresh, setRefresh] = useState(0)
  const [autoDiscovery, setAutoDiscovery] = useState(Boolean(provider && isLocalEndpoint(provider.base_url)))
  const [profiles, setProfiles] = useState<SavedProviderProfile[]>([])
  const [activeProfile, setActiveProfile] = useState('')
  const [profileName, setProfileName] = useState('')
  const [profileBusy, setProfileBusy] = useState(false)
  const sequence = useRef(0)
  const openrouter = isOpenRouter(form.base_url)
  const local = isLocalEndpoint(form.base_url)
  const discoveryKey = local ? key : ''
  const discoveryEnv = local ? form.api_key_env : ''
  const freeAllowance = usage?.free_model_daily_requests
  const sameSavedConnection = provider?.base_url === form.base_url && provider?.backend === form.backend
  const selectedModel = catalog?.models.find(model => model.id === form.model_id)

  useEffect(() => { dialog.current?.showModal() }, [])
  useEffect(() => {
    const controller = new AbortController()
    void api<{ profiles: SavedProviderProfile[]; active_profile?: string }>('/providers/profiles', 'GET', undefined, controller.signal)
      .then(result => { setProfiles(result.profiles); setActiveProfile(result.active_profile || '') })
      .catch(cause => { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : 'Cannot load saved provider profiles') })
    return () => controller.abort()
  }, [])
  useEffect(() => {
    const version = ++sequence.current
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    async function load() {
      if (tab !== 'cloud' || (!openrouter && !(local && autoDiscovery))) return
      setCatalog(null); setCatalogLoading(true); setCatalogError('')
      try {
        const result = openrouter
          ? await api<ModelCatalog>(`/providers/models?source=openrouter&refresh=${refresh > 0 ? 1 : 0}`, 'GET', undefined, controller.signal)
          : await api<ModelCatalog>('/providers/discover', 'POST', { backend: form.backend, base_url: form.base_url, api_key: discoveryKey || undefined, api_key_env: discoveryEnv || undefined }, controller.signal)
        if (version === sequence.current && !controller.signal.aborted) setCatalog(result)
      } catch (cause) {
        if (version === sequence.current && !controller.signal.aborted) setCatalogError(cause instanceof Error ? cause.message : 'Cannot load model metadata')
      } finally {
        if (version === sequence.current && !controller.signal.aborted) setCatalogLoading(false)
      }
    }
    timer = setTimeout(() => { void load() }, openrouter ? 0 : 450)
    return () => { sequence.current += 1; controller.abort(); clearTimeout(timer) }
  }, [tab, openrouter, local, autoDiscovery, form.backend, form.base_url, discoveryEnv, discoveryKey, refresh])

  function selectPreset(name: string) {
    if (name === 'custom') {
      setForm({ ...presets.openai, base_url: '', api_key_env: '', auto_context: true }); setKey(''); setCatalog(null); setCatalogError(''); setAutoDiscovery(false)
      return
    }
    const preset = presets[name as keyof typeof presets]
    if (!preset) return
    setForm({ ...preset, auto_context: true }); setKey(''); setCatalog(null); setCatalogError(''); setError('')
    setAutoDiscovery(name === 'local' || name === 'ollama')
  }
  function selectModel(model: CatalogModel) {
    const context = model.context_length && model.context_length > 0 ? model.context_length : 8192
    const output = Math.min(model.max_output_tokens && model.max_output_tokens > 0 ? model.max_output_tokens : 4096, context)
    setForm(previous => ({ ...previous, model_id: model.id, profile: model.profile || 'generic', context_length: context,
      max_tokens: output, input_cost_per_million: model.input_cost_per_million ?? null,
      output_cost_per_million: model.output_cost_per_million ?? null, request_cost_usd: model.request_cost_usd ?? null,
      auto_context: true, context_source: model.context_length ? catalog?.source || 'model_metadata' : 'unreported_default' }))
    setError('')
  }
  async function save(event: FormEvent) {
    event.preventDefault(); setSaving(true); setError('')
    try {
      if (!form.model_id.trim()) throw new Error('Choose a model or enter its exact ID in Advanced settings.')
      const result = await api<{ provider: Provider }>('/settings', 'POST', { ...form, auto_context: form.auto_context !== false, api_key: key || undefined, mode })
      setKey(''); saved(result.provider); close()
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Cannot save connection') }
    finally { setSaving(false) }
  }
  async function chooseProfile(id: string) {
    if (!id) return
    setProfileBusy(true); setError('')
    try {
      const result = await api<{ provider: Provider }>(`/providers/profiles/${encodeURIComponent(id)}/select`, 'POST', {})
      setForm(result.provider); setKey(''); setActiveProfile(id); setAutoDiscovery(isLocalEndpoint(result.provider.base_url)); saved(result.provider)
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Cannot select provider profile') }
    finally { setProfileBusy(false) }
  }
  async function saveProfile() {
    setProfileBusy(true); setError('')
    try {
      if (!profileName.trim() || !form.model_id.trim()) throw new Error('Choose a model and enter a profile name first.')
      const fields: (keyof Provider)[] = ['backend', 'model_id', 'base_url', 'profile', 'api_key_env', 'max_tokens', 'context_length', 'strategy_attempt_limit', 'input_cost_per_million', 'output_cost_per_million', 'request_cost_usd', 'auto_context', 'context_source']
      const safeProvider = Object.fromEntries(fields.filter(field => form[field] !== undefined).map(field => [field, form[field]]))
      await api('/providers/profiles/save', 'POST', { name: profileName.trim(), provider: safeProvider })
      const result = await api<{ profiles: SavedProviderProfile[]; active_profile?: string }>('/providers/profiles')
      setProfiles(result.profiles); setActiveProfile(result.active_profile || ''); setProfileName('')
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Cannot save provider profile') }
    finally { setProfileBusy(false) }
  }

  return <dialog ref={dialog} className={`settings-dialog connection-workspace ${tab === 'local' ? 'model-studio-dialog' : ''}`} onCancel={close} aria-labelledby="settings-title">
    <header><div><span className="connection-heading-icon"><KeyRound size={19} /></span><div><h2 id="settings-title">Model connections</h2><p>Choose where your agent runs.</p></div></div><button type="button" className="icon-button" aria-label="Close settings" onClick={close}><X size={18} /></button></header>
    <nav className="settings-tabs" aria-label="Model connection type"><button type="button" aria-pressed={tab === 'cloud'} className={tab === 'cloud' ? 'selected' : ''} onClick={() => setTab('cloud')}><Cloud size={17}/><span>Cloud & servers<small>Connect your provider</small></span></button><button type="button" aria-pressed={tab === 'local'} className={tab === 'local' ? 'selected' : ''} onClick={() => setTab('local')}><Cpu size={17}/><span>On-device models<small>Run on your hardware</small></span></button></nav>
    {tab === 'local' ? <LocalInferenceSettings saved={saved} /> : <form onSubmit={event => { void save(event) }}>
      <fieldset disabled={saving || profileBusy} style={{ border: 0, padding: 0, margin: 0, minWidth: 0 }}>
        <div className="connection-details-grid">
        <section className="connection-section" aria-labelledby="connection-provider-heading">
        <div className="connection-section-heading"><Server size={15}/><h3 id="connection-provider-heading">Connection</h3></div>
        <div className="form-row">{profiles.length > 0 && <label>Saved profile<select value={activeProfile} onChange={event => { void chooseProfile(event.target.value) }}><option value="">Current connection</option>{profiles.map(profile => <option key={profile.id} value={profile.id}>{profile.name}</option>)}</select></label>}
        <label>Provider<select value={presetFor(form)} onChange={event => selectPreset(event.target.value)}>{Object.entries(presets).map(([id, item]) => <option key={id} value={id}>{item.label}</option>)}<option value="custom">Custom connection</option></select></label></div>
        {!openrouter && <label>API endpoint<input required type="url" value={form.base_url} onChange={event => { setForm({ ...form, base_url: event.target.value, api_key_env: '' }); setKey(''); setAutoDiscovery(true); setCatalog(null) }} /></label>}
        </section>
        <section className="connection-section connection-auth" aria-labelledby="connection-auth-heading">
        <div className="connection-section-heading"><KeyRound size={15}/><h3 id="connection-auth-heading">Authentication</h3><span className={`connection-key-state ${sameSavedConnection && provider?.key_set ? 'available' : ''}`}>{sameSavedConnection && provider?.key_set ? <><Check size={12}/>Key available</> : key ? 'Unsaved key' : local ? 'Optional for local servers' : 'Bring your own key'}</span></div>
        <label>API key<input type="password" autoComplete="off" spellCheck={false} value={key} aria-describedby="connection-key-help" placeholder={sameSavedConnection && provider?.key_set ? 'Leave blank to keep the current key' : local ? 'Only if your server requires a key' : 'Paste your provider API key'} onChange={event => setKey(event.target.value)} /></label>
        <p id="connection-key-help" className="hint">Keys stay in this engine’s memory until it closes.</p>
        <details className="connection-environment"><summary>Use an environment variable{form.api_key_env && <code>{form.api_key_env}</code>}</summary><label>Environment variable name<input value={form.api_key_env || ''} placeholder={openrouter ? 'OPENROUTER_API_KEY' : 'PROVIDER_API_KEY'} autoComplete="off" spellCheck={false} onChange={event => setForm({ ...form, api_key_env: event.target.value })} /></label><p className="hint">Set this variable before launching Clin for a persistent connection. This field stores its name, never its value.</p></details>
        </section>
        </div>
        {openrouter && <>
          {sameSavedConnection && provider?.key_set ? <div role="status" className="hint">
            {freeAllowance ? `Free-model daily requests: ${freeAllowance.used} used / ${freeAllowance.limit}; ${freeAllowance.remaining} remaining.` : usage?.error || 'Daily free-model request allowance is not available.'}
            {freeAllowance && freeAllowance.remaining <= Math.max(5, Math.floor(freeAllowance.limit * 0.1)) && <p className="error-text">Free-model daily request allowance is low. Further use may need your approval.</p>}
          </div> : <p className="hint connection-catalog-note">Browse the full OpenRouter catalog without a key. Save your connection to load account allowances.</p>}
        </>}
        {local && <div className="connection-discovery"><p className="hint">Find models on this local server. Detection reads metadata without generating tokens.</p><button type="button" className="connection-secondary" disabled={catalogLoading} onClick={() => { setAutoDiscovery(true); setRefresh(value => value + 1) }}>Detect models</button></div>}
        {(openrouter || local) && <ModelCatalogPicker catalog={catalog} selected={form.model_id} loading={catalogLoading} error={catalogError} choose={selectModel} refresh={openrouter ? () => setRefresh(value => value + 1) : undefined} disabled={saving} initialFreeOnly={preferFree} />}
        {!openrouter && !local && <section className="connection-section"><h3>Model</h3><label>Model ID<input required value={form.model_id} placeholder="Enter the exact model ID from your provider" onChange={event => setForm({ ...form, model_id: event.target.value, input_cost_per_million: null, output_cost_per_million: null, request_cost_usd: null, context_source: 'manual_model_id' })} /></label></section>}
        {form.model_id && <section className="connection-selection" aria-label="Selected model"><div><span>Selected model</span><strong>{selectedModel?.name || form.model_id}</strong><code>{form.model_id}</code></div><span className="connection-selection-mode">{selectedModel?.is_free === true ? 'Free' : selectedModel?.is_free === false ? 'Metered' : local ? 'Local server' : 'Pricing unknown'}</span><p>{form.context_length?.toLocaleString() || 'Provider default'} context tokens<span>{form.auto_context !== false ? 'Context adapts to your mode' : 'Manual context limits'}</span></p></section>}
        {contextPolicy && sameSavedConnection && <details className="connection-context"><summary>Current context allocation</summary><dl><div><dt>Model window</dt><dd>{contextPolicy.model_limit?.toLocaleString() ?? 'Unknown'} tokens</dd></div><div><dt>Prompt allowance</dt><dd>{contextPolicy.prompt_tokens?.toLocaleString() ?? 'Unknown'}</dd></div><div><dt>Output allowance</dt><dd>{contextPolicy.completion_tokens?.toLocaleString() ?? 'Unknown'}</dd></div><div><dt>Memory allowance</dt><dd>{contextPolicy.memory_chars?.toLocaleString() ?? 'Unknown'} characters</dd></div></dl><p className="hint">For {contextPolicy.mode || mode}. Source: {contextPolicy.source || 'Not reported'}. Prompt allowances use conservative UTF-8 byte bounds, rather than exact tokenizer counts.</p></details>}
        <details style={{ margin: '16px 0' }}>
          <summary>Advanced settings and manual overrides</summary>
          <label>API protocol<select value={form.backend} onChange={event => setForm({ ...form, backend: event.target.value })}><option value="openai_compatible">OpenAI-compatible</option><option value="anthropic">Anthropic Messages</option><option value="ollama">Ollama</option></select></label>
          {(openrouter || local) && <label>Exact model ID<input value={form.model_id} placeholder="Model ID available on your account or local server" onChange={event => setForm({ ...form, model_id: event.target.value, input_cost_per_million: null, output_cost_per_million: null, request_cost_usd: null, context_source: 'manual_model_id' })} /></label>}
          {openrouter && <label>API endpoint<input required type="url" value={form.base_url} onChange={event => { setForm({ ...form, base_url: event.target.value, api_key_env: '' }); setKey(''); setAutoDiscovery(true); setCatalog(null) }} /></label>}
          <label style={{ flexDirection: 'row', alignItems: 'center' }}><input type="checkbox" checked={form.auto_context !== false} onChange={event => setForm({ ...form, auto_context: event.target.checked })} />Automatically use model metadata and adapt context to the current mode</label>
          <div className="form-row"><label>Context window tokens<input type="number" min={512} step={1} value={form.context_length ?? ''} placeholder="From model metadata" disabled={form.auto_context !== false} onChange={event => setForm({ ...form, context_length: Number(event.target.value) })} /></label><label>Output token limit<input type="number" min={128} step={1} value={form.max_tokens || 4096} disabled={form.auto_context !== false} onChange={event => setForm({ ...form, max_tokens: Number(event.target.value) })} /></label></div>
          <label>Harness profile<select value={form.profile || 'generic'} onChange={event => setForm({ ...form, profile: event.target.value })}><option value="generic">Standard / automatic family</option><option value="glm53">GLM 5.3</option><option value="qwen38">Qwen 3.8</option><option value="openrouter_deepseek">OpenRouter · DeepSeek</option><option value="openrouter_glm">OpenRouter · GLM</option><option value="openrouter_kimi">OpenRouter · Kimi</option></select></label>
          <label>Recovery strategy attempts<input type="number" min={1} max={10} value={form.strategy_attempt_limit || 3} onChange={event => setForm({ ...form, strategy_attempt_limit: Number(event.target.value) })} /></label>
          <div className="form-row"><label>Input price · USD per million tokens<input type="number" min={0} step="any" value={form.input_cost_per_million ?? ''} placeholder="Unknown" onChange={event => setForm({ ...form, input_cost_per_million: event.target.value === '' ? null : Number(event.target.value) })} /></label><label>Output price · USD per million tokens<input type="number" min={0} step="any" value={form.output_cost_per_million ?? ''} placeholder="Unknown" onChange={event => setForm({ ...form, output_cost_per_million: event.target.value === '' ? null : Number(event.target.value) })} /></label></div>
          <p className="hint">Manual token limits are retained when automatic context is off. A strict dollar cap needs known pricing. Blank prices are unknown; zero means free.</p>
        </details>
        <details className="saved-provider-profiles"><summary>Save this connection as a named profile</summary><p className="hint">Profiles store model settings and environment variable names. Raw API keys are never included.</p><label>Profile name<input value={profileName} maxLength={80} placeholder="Free coding model, Local review, Planning…" onChange={event => setProfileName(event.target.value)} /></label><button type="button" disabled={profileBusy || !profileName.trim() || !form.model_id.trim()} onClick={() => { void saveProfile() }}>Save profile</button></details>
        <footer className="connection-save-bar"><div>{error && <p role="alert" className="error-text">{error}</p>}<p>Saving does not start a model request.</p></div><button type="submit" className="primary" disabled={saving || !form.model_id.trim()}>{saving ? 'Saving…' : 'Save connection'}</button></footer>
      </fieldset>
    </form>}
  </dialog>
}
