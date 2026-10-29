import { useEffect, useRef, useState } from 'react'
import { Box, Check, Cpu, Gauge, HardDrive, Play, Plus, RefreshCw, Search, ShieldCheck, SlidersHorizontal, Square } from 'lucide-react'
import { modelName, modelQuantization, modelSize, runtimePresentation } from '../localModels'
import { api } from '../api'
import { startSerialPolling } from '../polling'
import { InferenceTuningPanel } from './InferenceTuningPanel'
import type { Provider } from '../api'

type Settings = { runtime_path: string; model_path: string; draft_model_path: string; context_size: number; gpu_layers: number; threads: number; flash_attention: string; prompt_cache: boolean; cache_ram_mb: number; prefetch: boolean; prefetch_mb: number; startup_timeout: number }
type Status = { state: string; ready: boolean; settings: Settings; error?: string; capabilities: Record<string, unknown>; prefetch: Record<string, unknown>; profiling: Record<string, unknown>; installation: Record<string, unknown> }
type Candidate = { path: string; name: string; display_name?: string; size_bytes: number; valid: boolean; validity: string; context_length?: number | null; recommended_context_size?: number; architecture?: string | null; metadata_partial?: boolean; libraries?: { name: string }[] }
type Discovery = { state: string; started: boolean; models: Candidate[]; runtimes: Candidate[]; scanned_entries: number; errors: number; truncated: boolean; current_path?: string; manifest_path?: string; limits?: { max_entries: number; max_seconds: number }; computer_roots?: string[] }
type Selection = Partial<Settings> & { settings?: Partial<Settings>; selection?: Partial<Settings> }

export function LocalInferenceSettings({ saved }: { saved: (provider: Provider) => void }) {
  const [status, setStatus] = useState<Status | null>(null), [form, setForm] = useState<Settings | null>(null)
  const [error, setError] = useState(''), [busy, setBusy] = useState(false), [discovery, setDiscovery] = useState<Discovery | null>(null)
  const [pollError, setPollError] = useState('')
  const revision = useRef(0), commandPending = useRef(false), mounted = useRef(false)
  const [showUnsupported, setShowUnsupported] = useState(false)
  const [search, setSearch] = useState(''), [tab, setTab] = useState<'models' | 'runtime' | 'performance'>('models'), [page, setPage] = useState(0), [automaticContext, setAutomaticContext] = useState(true)
  useEffect(() => {
    let active = true, requestedScan = false
    mounted.current = true
    const stop = startSerialPolling(async signal => {
      if (commandPending.current) return
      const version = revision.current
      const errors: string[] = []
      await Promise.all([
        api<Status>('/inference', 'GET', undefined, signal).then(result => {
          if (active && version === revision.current) { setStatus(result); setForm(previous => previous ? { ...previous, runtime_path: previous.runtime_path || result.settings.runtime_path } : result.settings) }
        }).catch(problem => { errors.push(String(problem)) }),
        api<Discovery>('/inference/discovery', 'GET', undefined, signal).then(async result => {
          if (!active || version !== revision.current) return
          setDiscovery(result)
          if (!result.started && !requestedScan) { requestedScan = true; const started = await api<Discovery>('/inference/discover', 'POST', {}, signal); if (active && version === revision.current) setDiscovery(started) }
        }).catch(problem => { errors.push(String(problem)) }),
      ])
      if (active && version === revision.current) setPollError(errors.join(' · '))
    }, 1500, problem => { if (active) setPollError(String(problem)) })
    return () => { active = false; mounted.current = false; stop() }
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
    commandPending.current = true; revision.current += 1
    setBusy(true); setError('')
    try {
      const result = await api<Status>(`/inference/${action}`, 'POST', action === 'configure' || action === 'start' ? form : {})
      if (!mounted.current) return
      setStatus(result); if (action === 'install-runtime' || action === 'stop') setForm(result.settings)
      if (result.ready) { const settings = await api<{ provider: Provider }>('/settings'); if (mounted.current && settings.provider) saved(settings.provider) }
    } catch (problem) { if (mounted.current) setError(problem instanceof Error ? problem.message : String(problem)) }
    finally { commandPending.current = false; if (mounted.current) setBusy(false) }
  }
  if (!form || !status) return <p className="hint" role={error || pollError ? 'alert' : 'status'}>{error || pollError || 'Opening your local model library…'}</p>
  const running = ['running', 'starting', 'stopping', 'installing'].includes(status.state)
  const scanning = discovery?.state === 'scanning', kind = tab === 'runtime' ? 'runtime' : 'model'
  const candidates = (kind === 'runtime' ? discovery?.runtimes : discovery?.models || []) || []
  const filtered = candidates.filter(item => (showUnsupported || item.valid) && `${item.path} ${item.display_name || item.name} ${item.architecture || ''}`.toLowerCase().includes(search.toLowerCase()))
  const pages = Math.max(1, Math.ceil(filtered.length / 8)), currentPage = Math.min(page, pages - 1)
  const selectedModel = discovery?.models.find(item => item.path === form.model_path)
  const loaded = runtimePresentation(status.state, status.ready, status.settings.model_path)
  const locked = running || busy
  return <section className="local-inference local-model-studio" aria-label="Local model studio">
    <header className="model-studio-header"><div><span className="model-studio-icon"><Cpu size={20}/></span><div><h2>Local models</h2><p>Your models. Your hardware.</p></div></div><span className="local-only-badge"><ShieldCheck size={13}/>On your computer</span></header>
    <section className={`loaded-model-bar ${loaded.tone}`} aria-label="Loaded model" aria-live="polite">
      <span className="model-state-light"/><div><strong>{loaded.model}</strong><span>{loaded.label}{status.ready ? ` · ${status.settings.context_size.toLocaleString()} context tokens` : ''}</span></div>
      {['running', 'starting', 'stopping'].includes(status.state)
        ? <button type="button" disabled={busy || status.state === 'stopping'} onClick={() => { void command('stop') }}><Square size={13}/>{status.state === 'stopping' ? 'Unloading…' : 'Unload'}</button>
        : <button type="button" className="primary" disabled={locked || !form.model_path} onClick={() => { if (!form.runtime_path) { setTab('runtime'); setSearch(''); setPage(0) } else void command('start') }}><Play size={13}/>{form.runtime_path ? 'Load model' : 'Choose runtime'}</button>}
    </section>
    {(error || status.error || pollError) && <p className="error-text model-studio-error" role="alert">{error || status.error || pollError}</p>}
    <nav className="model-studio-tabs" aria-label="Local model views">{([['models', 'My models', HardDrive], ['runtime', 'Runtime', Cpu], ['performance', 'Performance', Gauge]] as const).map(([id, label, Icon]) => <button type="button" aria-pressed={tab === id} className={tab === id ? 'active' : ''} key={id} onClick={() => { setTab(id); setSearch(''); setPage(0) }}><Icon size={14}/>{label}{id === 'models' && <span>{discovery?.models.filter(item => item.valid).length || 0}</span>}</button>)}</nav>
    {tab === 'performance' ? <InferenceTuningPanel /> : <div className="local-model-workspace">
      <section className="local-model-library" aria-label={kind === 'model' ? 'Model library' : 'Runtime library'}>
        <div className="local-library-heading"><h3>{kind === 'model' ? 'Your model library' : 'Inference runtime'}</h3><button type="button" disabled={scanning} onClick={() => { void discover() }} title="Search common model and runtime folders"><RefreshCw size={13}/>{scanning ? 'Scanning…' : 'Rescan'}</button></div>
        <label className="local-library-search"><Search size={15}/><input aria-label="Search local library" value={search} placeholder={kind === 'model' ? 'Search your models…' : 'Search runtimes…'} onChange={event => { setSearch(event.target.value); setPage(0) }} /></label>
        {scanning && <div className="local-scan-progress" role="status"><progress aria-label="Searching local files"/><span>{discovery?.scanned_entries.toLocaleString()} files checked</span><button type="button" onClick={() => { void api<Discovery>('/inference/discovery/cancel', 'POST', {}).then(setDiscovery).catch(problem => setError(String(problem))) }}>Cancel</button></div>}
        <div className="local-model-list">{filtered.slice(currentPage * 8, (currentPage + 1) * 8).map(item => {
          const selected = (kind === 'model' ? form.model_path : form.runtime_path) === item.path
          const isLoaded = kind === 'model' && status.ready && status.settings.model_path === item.path
          return <button type="button" className={`local-model-item ${selected ? 'selected' : ''}`} key={item.path} disabled={!item.valid || locked} aria-pressed={selected} onClick={() => { void choose(item, kind) }}>
            <span className="local-file-icon">{kind === 'model' ? <Box size={19}/> : <Cpu size={19}/>}</span>
            <span className="local-model-copy"><strong>{item.display_name || item.name}</strong><span className="local-model-tags"><span>{kind === 'model' ? modelQuantization(item.path) || 'GGUF' : 'llama.cpp'}</span>{item.architecture && <span>{item.architecture}</span>}<span>{modelSize(item.size_bytes)}</span>{isLoaded && <span className="loaded-tag">Loaded</span>}</span><small title={item.path}>{item.path}</small>{!item.valid && <small className="error-text">{item.validity.replaceAll('_', ' ')}</small>}</span>
            {selected && <Check size={16} className="local-selected-check"/>}
          </button>
        })}</div>
        {!filtered.length && <div className="local-library-empty"><HardDrive size={27}/><strong>{search ? 'No matching files' : scanning ? 'Looking for local files' : kind === 'model' ? 'Add your first model' : 'Choose a runtime'}</strong><p>{kind === 'model' ? 'Import an existing GGUF file or search your computer. Models stay on your device.' : 'Use a detected llama-server binary or install the official runtime.'}</p><button type="button" disabled={locked || !window.localCoder?.chooseInferenceFile} onClick={() => { void browse(kind) }}><Plus size={14}/>Browse files</button></div>}
        <footer className="local-library-footer"><span>{filtered.length} {kind === 'model' ? 'models' : 'runtimes'}</span><div><button type="button" aria-label="Previous local models" disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)}>Previous</button><span>{currentPage + 1} / {pages}</span><button type="button" aria-label="Next local models" disabled={currentPage + 1 >= pages} onClick={() => setPage(currentPage + 1)}>Next</button></div></footer>
        <details className="local-discovery-details"><summary>Find more files</summary><label className="local-show-unsupported"><input type="checkbox" checked={showUnsupported} onChange={event => { setShowUnsupported(event.target.checked); setPage(0) }}/>Show unsupported and vocabulary files</label><p>Search includes common model folders and Hugging Face caches. Found executables are not started by discovery.</p><button type="button" disabled={scanning} onClick={() => { void discover(true) }}>Search all folders</button>{discovery?.truncated && <p role="status">Search reached its limit; these results are partial.</p>}{discovery && <p>{discovery.scanned_entries.toLocaleString()} entries checked · {discovery.errors} unreadable or invalid</p>}{discovery?.manifest_path && <p className="local-path">Path manifest: {discovery.manifest_path}</p>}</details>
      </section>
      <aside className="local-load-settings" aria-label="Model load settings">
        <div className="local-settings-heading"><SlidersHorizontal size={16}/><h3>Load settings</h3></div>
        <div className="local-selected-model"><span>Selected model</span><strong title={form.model_path}>{selectedModel?.display_name || modelName(form.model_path)}</strong>{selectedModel?.context_length && <small>Native context · {selectedModel.context_length.toLocaleString()} tokens</small>}</div>
        <label>Context length<div className="local-unit-input"><input aria-label="Context length" type="number" min={512} max={1048576} disabled={locked || automaticContext} value={form.context_size} onChange={event => setForm({ ...form, context_size: Number(event.target.value) })}/><span>tokens</span></div></label>
        <label className="runtime-checkbox"><input type="checkbox" checked={automaticContext} disabled={locked} onChange={event => { setAutomaticContext(event.target.checked); if (event.target.checked && selectedModel?.recommended_context_size) setForm({ ...form, context_size: selectedModel.recommended_context_size }) }}/>Choose context automatically</label>
        <label>GPU offload<select aria-label="GPU offload" disabled={locked} value={[-1, 0].includes(form.gpu_layers) ? String(form.gpu_layers) : 'custom'} onChange={event => { setForm({ ...form, gpu_layers: event.target.value === 'custom' ? 16 : Number(event.target.value) }) }}><option value="0">CPU only</option><option value="-1">All supported GPU layers</option><option value="custom">Custom layer count</option></select></label>
        <label>GPU layers<input type="number" min={-1} max={999} disabled={locked} value={form.gpu_layers} onChange={event => setForm({ ...form, gpu_layers: Number(event.target.value) })}/></label>
        <label>CPU threads<input type="number" min={1} max={256} disabled={locked} value={form.threads} onChange={event => setForm({ ...form, threads: Number(event.target.value) })}/></label>
        <label className="runtime-checkbox"><input type="checkbox" checked={form.prompt_cache} disabled={locked} onChange={event => setForm({ ...form, prompt_cache: event.target.checked })}/>Reuse prompt cache</label>
        <label className="runtime-checkbox"><input type="checkbox" checked={form.prefetch} disabled={locked} onChange={event => setForm({ ...form, prefetch: event.target.checked })}/>Prefetch model pages</label>
        <details className="local-file-settings" open={tab === 'runtime'}><summary>Model files & runtime</summary>{(['model', 'draft', 'runtime'] as const).map(purpose => { const key = purpose === 'runtime' ? 'runtime_path' : purpose === 'draft' ? 'draft_model_path' : 'model_path'; return <label key={purpose}>{purpose === 'model' ? 'Model file' : purpose === 'draft' ? 'Draft model (optional)' : 'Runtime executable'}<div className="path-picker"><input value={form[key]} disabled={locked} placeholder={purpose === 'runtime' ? 'Path to llama-server' : 'Path to .gguf'} onChange={event => setForm({ ...form, [key]: event.target.value })}/><button type="button" aria-label={`Browse ${purpose} file`} disabled={locked || !window.localCoder?.chooseInferenceFile} onClick={() => { void browse(purpose) }}>Browse</button></div></label> })}<button type="button" disabled={locked} onClick={() => { void command('install-runtime') }}>Install official runtime</button></details>
        <p className="local-settings-note">Unload before changing settings. Context and GPU offload depend on available memory; a compatible draft model can enable speculative decoding.</p>
        <button type="button" className="local-save-settings" disabled={locked} onClick={() => { void command('configure') }}>Save load settings</button>
      </aside>
    </div>}
    <details className="local-runtime-diagnostics"><summary>Runtime diagnostics</summary><pre>{JSON.stringify({ capabilities: status.capabilities, prefetch: status.prefetch, profiling: status.profiling, installation: status.installation }, null, 2)}</pre></details>
  </section>
}
