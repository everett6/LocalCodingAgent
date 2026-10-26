import { useEffect, useState } from 'react'
import { api } from '../api'
import type { Provider } from '../api'

type Settings = { runtime_path: string; model_path: string; draft_model_path: string; context_size: number; gpu_layers: number; threads: number; flash_attention: string; prompt_cache: boolean; cache_ram_mb: number; prefetch: boolean; prefetch_mb: number; startup_timeout: number }
type Status = { state: string; ready: boolean; settings: Settings; error?: string; capabilities: Record<string, unknown>; prefetch: Record<string, unknown>; profiling: Record<string, unknown>; installation: Record<string, unknown> }
type Candidate = { path: string; name: string; display_name?: string; size_bytes: number; valid: boolean; validity: string; context_length?: number | null; recommended_context_size?: number; architecture?: string | null; metadata_partial?: boolean; libraries?: { name: string }[] }
type Discovery = { state: string; started: boolean; models: Candidate[]; runtimes: Candidate[]; scanned_entries: number; errors: number; truncated: boolean; current_path?: string; manifest_path?: string; limits?: { max_entries: number; max_seconds: number }; computer_roots?: string[] }
type Selection = Partial<Settings> & { settings?: Partial<Settings>; selection?: Partial<Settings> }
const sizeLabel = (bytes: number) => bytes >= 1073741824 ? `${(bytes / 1073741824).toFixed(2)} GiB` : `${(bytes / 1048576).toFixed(1)} MiB`

export function LocalInferenceSettings({ saved }: { saved: (provider: Provider) => void }) {
  const [status, setStatus] = useState<Status | null>(null), [form, setForm] = useState<Settings | null>(null)
  const [error, setError] = useState(''), [busy, setBusy] = useState(false), [discovery, setDiscovery] = useState<Discovery | null>(null)
  const [search, setSearch] = useState(''), [kind, setKind] = useState('all'), [page, setPage] = useState(0), [automaticContext, setAutomaticContext] = useState(true)
  useEffect(() => {
    let active = true, requestedScan = false
    const load = () => {
      void api<Status>('/inference').then(result => { if (active) { setStatus(result); setForm(previous => previous ? { ...previous, runtime_path: previous.runtime_path || result.settings.runtime_path } : result.settings) } }).catch(problem => { if (active) setError(String(problem)) })
      void api<Discovery>('/inference/discovery').then(async result => {
        if (!active) return
        setDiscovery(result)
        if (!result.started && !requestedScan) { requestedScan = true; const started = await api<Discovery>('/inference/discover', 'POST', {}); if (active) setDiscovery(started) }
      }).catch(problem => { if (active) setError(String(problem)) })
    }
    load(); const timer = setInterval(load, 1500)
    return () => { active = false; clearInterval(timer) }
  }, [])
  async function browse(purpose: 'runtime' | 'model' | 'draft') {
    try { const path = await window.localCoder?.chooseInferenceFile?.(purpose); if (path && form) setForm({ ...form, [purpose === 'runtime' ? 'runtime_path' : purpose === 'draft' ? 'draft_model_path' : 'model_path']: path }) } catch (problem) { setError(String(problem)) }
  }
  async function discover(all = false) {
    setError('')
    try { setDiscovery(await api<Discovery>('/inference/discover', 'POST', all ? { roots: discovery?.computer_roots || ['/'] } : {})); setPage(0) } catch (problem) { setError(String(problem)) }
  }
  async function choose(item: Candidate, type: 'model' | 'runtime') {
    setBusy(true); setError('')
    try {
      const result = await api<Selection>('/inference/discovery/select', 'POST', { [type === 'model' ? 'model_path' : 'runtime_path']: item.path })
      const patch = result.settings || result.selection || result
      setForm(previous => previous ? { ...previous, ...(typeof patch.runtime_path === 'string' ? { runtime_path: patch.runtime_path } : {}), ...(typeof patch.model_path === 'string' ? { model_path: patch.model_path } : {}), ...(automaticContext && typeof patch.context_size === 'number' ? { context_size: patch.context_size } : {}) } : previous)
    } catch (problem) { setError(String(problem)) } finally { setBusy(false) }
  }
  async function command(action: string) {
    setBusy(true); setError('')
    try {
      const result = await api<Status>(`/inference/${action}`, 'POST', action === 'configure' || action === 'start' ? form : {})
      setStatus(result); if (action === 'install-runtime' || action === 'stop') setForm(result.settings)
      if (result.ready) { const settings = await api<{ provider: Provider }>('/settings'); if (settings.provider) saved(settings.provider) }
    } catch (problem) { setError(problem instanceof Error ? problem.message : String(problem)) } finally { setBusy(false) }
  }
  if (!form || !status) return <p className="hint">{error || 'Reading local runtime settings…'}</p>
  const running = ['running', 'starting', 'stopping', 'installing'].includes(status.state), scanning = discovery?.state === 'scanning'
  const candidates = [...(discovery?.runtimes || []).map(item => ({ ...item, type: 'runtime' as const })), ...(discovery?.models || []).map(item => ({ ...item, type: 'model' as const }))].filter(item => (kind === 'all' || kind === item.type) && `${item.path} ${item.display_name || item.name} ${item.architecture || ''} ${item.validity}`.toLowerCase().includes(search.toLowerCase()))
  const pages = Math.max(1, Math.ceil(candidates.length / 10)), currentPage = Math.min(page, pages - 1), selectedModel = discovery?.models.find(item => item.path === form.model_path)
  return <section className="local-inference">
    <div className="runtime-summary"><p className="runtime-state">Runtime: <strong>{status.state}</strong></p><p className="hint">Run an existing local GGUF model with llama.cpp. Discovery reads headers and locations without starting found programs or downloading weights.</p></div>
    <section className="section-card model-catalog discovery-catalog" aria-label="Detected local models and runtimes">
      <h3>Models and runtimes on this computer</h3><p className="hint">Common locations are searched automatically, including Hugging Face caches. Detected binaries are checked for runtime health only when you choose Start.</p>
      <div className="runtime-actions"><button type="button" disabled={scanning} onClick={() => { void discover() }}>Search common locations</button><button type="button" disabled={scanning} onClick={() => { void discover(true) }}>Search all folders</button>{scanning && <button type="button" onClick={() => { void api<Discovery>('/inference/discovery/cancel', 'POST', {}).then(setDiscovery).catch(problem => setError(String(problem))) }}>Cancel search</button>}</div>
      {discovery && <p className="discovery-progress" aria-live="polite">{discovery.state} · {discovery.scanned_entries.toLocaleString()} entries · {discovery.models.length} GGUF files · {discovery.runtimes.length} runtime candidates{discovery.errors > 0 ? ` · ${discovery.errors} unreadable or invalid entries` : ''}</p>}
      {scanning && <><progress aria-label="Searching local files" /><p className="hint">{discovery?.current_path}</p></>}
      {discovery?.truncated && <p className="error-text" role="status">Search reached its explicit limit ({discovery.limits?.max_entries.toLocaleString()} entries / {discovery.limits?.max_seconds} seconds). Results are partial; search a narrower location or extend the search limits.</p>}
      <div className="form-row"><label>Search detected files<input value={search} placeholder="Name, folder or architecture" onChange={event => { setSearch(event.target.value); setPage(0) }} /></label><label>Show<select value={kind} onChange={event => { setKind(event.target.value); setPage(0) }}><option value="all">All detected files</option><option value="model">GGUF files</option><option value="runtime">Runtime candidates</option></select></label></div>
      <div className="discovery-list">{candidates.slice(currentPage * 10, (currentPage + 1) * 10).map(item => <div className="discovery-row" key={`${item.type}:${item.path}`}>
        <div><strong>{item.display_name || item.name}</strong> <span className="status-badge">{item.valid ? item.type === 'runtime' ? 'Detected · not started' : 'GGUF header checked' : item.validity.replaceAll('_', ' ')}</span><p className="hint">{sizeLabel(item.size_bytes)}{item.architecture ? ` · ${item.architecture}` : ''}{item.context_length ? ` · Native context ${item.context_length.toLocaleString()}` : item.type === 'model' ? ' · Native context unknown' : ` · ${item.libraries?.length || 0} adjacent libraries`}{item.metadata_partial ? ' · Metadata inspection bounded' : ''}</p><p className="hint">{item.path}</p></div>
        <button type="button" disabled={!item.valid || running || busy} onClick={() => { void choose(item, item.type) }}>{(item.type === 'model' ? form.model_path : form.runtime_path) === item.path ? 'Selected' : item.type === 'model' ? 'Choose model' : 'Choose runtime'}</button>
      </div>)}</div>
      {!candidates.length && <p className="hint">{scanning ? 'Looking for supported GGUF headers and llama-server binaries…' : 'No matching files. Search all folders or choose a file manually below.'}</p>}
      <div className="runtime-actions"><button type="button" disabled={currentPage === 0} onClick={() => setPage(Math.max(0, currentPage - 1))}>Previous</button><span>{candidates.length} results · Page {currentPage + 1} of {pages}</span><button type="button" disabled={currentPage + 1 >= pages} onClick={() => setPage(currentPage + 1)}>Next</button></div>
      {discovery?.manifest_path && <p className="hint">Protected path manifest: {discovery.manifest_path}. It contains locations only and grants no access outside the project.</p>}
    </section>
    {(['model', 'draft', 'runtime'] as const).map(purpose => { const key = purpose === 'runtime' ? 'runtime_path' : purpose === 'draft' ? 'draft_model_path' : 'model_path'; return <label key={purpose}>{purpose === 'model' ? 'Model file (.gguf)' : purpose === 'draft' ? 'Optional draft model (.gguf)' : 'Runtime executable'}<div className="path-picker"><input value={form[key]} disabled={running || busy} placeholder={purpose === 'runtime' ? 'Choose a detected runtime or install llama-server' : 'Choose an existing local GGUF file'} onChange={event => setForm({ ...form, [key]: event.target.value })} /><button type="button" disabled={running || busy || !window.localCoder?.chooseInferenceFile} onClick={() => { void browse(purpose) }}>Browse</button></div></label> })}
    <label className="runtime-checkbox"><input type="checkbox" checked={automaticContext} disabled={running || busy} onChange={event => { setAutomaticContext(event.target.checked); if (event.target.checked && selectedModel?.recommended_context_size) setForm({ ...form, context_size: selectedModel.recommended_context_size }) }} />Use conservative context based on GGUF metadata and available RAM</label>
    <p className="hint">Selected context: {form.context_size.toLocaleString()} tokens. {selectedModel?.context_length ? `Model metadata reports ${selectedModel.context_length.toLocaleString()} native context tokens.` : 'The model maximum is unknown until metadata is available.'} The memory estimate does not guarantee that every model will fit.</p>
    <details><summary>Advanced local runtime settings</summary><div className="form-row"><label>Context tokens<input type="number" min={512} max={1048576} disabled={running || busy || automaticContext} value={form.context_size} onChange={event => setForm({ ...form, context_size: Number(event.target.value) })} /></label><label>GPU layers (0 = CPU, −1 = all)<input type="number" min={-1} max={999} disabled={running || busy} value={form.gpu_layers} onChange={event => setForm({ ...form, gpu_layers: Number(event.target.value) })} /></label></div><p className="hint">Turn off automatic context to supply an explicit override. Larger context needs additional KV-cache memory.</p></details>
    <label className="runtime-checkbox"><input type="checkbox" checked={form.prefetch} disabled={running || busy} onChange={event => setForm({ ...form, prefetch: event.target.checked })} />Prefetch selected model pages</label><label className="runtime-checkbox"><input type="checkbox" checked={form.prompt_cache} disabled={running || busy} onChange={event => setForm({ ...form, prompt_cache: event.target.checked })} />Reuse prompt and KV cache</label>
    <p className="hint">A compatible draft model enables native speculative decoding when supported. Features are checked against the installed runtime; performance depends on your model and hardware.</p>
    <div className="runtime-actions"><button type="button" disabled={running || busy} onClick={() => { void command('install-runtime') }}>Install official runtime</button><button type="button" disabled={running || busy} onClick={() => { void command('configure') }}>Save & prefetch</button>{running ? <button type="button" disabled={busy} onClick={() => { void command('stop') }}>Stop runtime</button> : <button type="button" className="primary" disabled={busy || !form.model_path} onClick={() => { void command('start') }}>Start & use model</button>}</div>
    {(error || status.error) && <p className="error-text" role="alert">{error || status.error}</p>}<details><summary>Runtime features & performance</summary><pre>{JSON.stringify({ capabilities: status.capabilities, prefetch: status.prefetch, profiling: status.profiling, installation: status.installation }, null, 2)}</pre></details>
  </section>
}
