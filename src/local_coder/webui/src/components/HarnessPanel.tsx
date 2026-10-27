import { useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { api } from '../api'
import { ContextAccountingPanel } from './ContextAccountingPanel'
import { ReadinessPanel } from './ReadinessPanel'
import type { Readiness } from './ReadinessPanel'

type Settings = {
  memory_enabled: boolean
  memory_context_chars: number
  language_context_enabled: boolean
  pre_edit_snapshots: boolean
  max_requests: number
  max_tokens: number
  max_request_tokens: number
  max_cost_usd: number | null
  max_subagents: number
  profile_overrides: Record<string, unknown>
  sandbox_backend: string
  sandbox_image: string
}
type Revision = {
  revision_id: string
  kind: string
  state: string
  created_ns?: number
  payload: unknown
  events?: unknown[]
}
type Budget = {
  requests: number
  tokens: number
  cost_usd: number
  cost_known: boolean
  limits?: { max_requests?: number; max_tokens?: number; max_cost_usd?: number | null }
}
type HarnessStatus = {
  readiness?: Readiness
  settings: Settings
  safety_tier: string
  budget: Budget | null
  sandbox: { backend?: string; ready?: boolean; reason?: string; network?: string; host_filesystem?: string }
  evolution: Revision[]
  language?: { adapter_count?: number; languages?: { server_available?: boolean; server_running?: boolean }[]; note?: string }
}
type Snapshot = { id: string; created_at: string | number; paths: string[]; status: string }
type SnapshotDiff = { id: string; files: { path: string; before: string; after: string }[]; diff: string }
type NumberField = 'max_requests' | 'max_tokens' | 'max_request_tokens' | 'max_subagents' | 'memory_context_chars'
type Draft = Record<NumberField, string> & {
  max_cost_usd: string
  memory_enabled: boolean
  language_context_enabled: boolean
  profile_overrides: string
  safety_tier: string
}

const numericFields: { key: NumberField; label: string; min: number; max: number }[] = [
  { key: 'max_requests', label: 'Model calls per task', min: 1, max: 10000 },
  { key: 'max_tokens', label: 'Shared token cap', min: 1024, max: 100000000 },
  { key: 'max_request_tokens', label: 'Token cap per model call', min: 1024, max: 2000000 },
  { key: 'max_subagents', label: 'Maximum child agents', min: 0, max: 3 },
  { key: 'memory_context_chars', label: 'Memory context characters per step', min: 0, max: 12000 },
]
const tiers = [
  ['tier_1_paranoia', 'Tier 1 · Read only'],
  ['tier_2_strict', 'Tier 2 · Confirm changes and commands'],
  ['tier_3_balanced', 'Tier 3 · Balanced approvals'],
  ['tier_4_auto_valid', 'Tier 4 · Automatic validated generation'],
  ['tier_5_yolo', 'Tier 5 · Autonomous generation'],
]

function draftFrom(status: HarnessStatus): Draft {
  const settings = status.settings
  return {
    max_requests: String(settings.max_requests), max_tokens: String(settings.max_tokens),
    max_request_tokens: String(settings.max_request_tokens), max_subagents: String(settings.max_subagents),
    memory_context_chars: String(settings.memory_context_chars),
    max_cost_usd: settings.max_cost_usd == null ? '' : String(settings.max_cost_usd),
    memory_enabled: settings.memory_enabled, language_context_enabled: settings.language_context_enabled,
    profile_overrides: JSON.stringify(settings.profile_overrides || {}, null, 2), safety_tier: status.safety_tier,
  }
}

function dateLabel(value: string | number): string {
  const date = new Date(typeof value === 'number' ? value * 1000 : value)
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString()
}

export function HarnessPanel({ disabled = false }: { disabled?: boolean }) {
  const [tab, setTab] = useState<'controls' | 'snapshots' | 'revisions'>('controls')
  const [status, setStatus] = useState<HarnessStatus | null>(null)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [snapshots, setSnapshots] = useState<Snapshot[]>([])
  const [comparison, setComparison] = useState<SnapshotDiff | null>(null)
  const [confirmRestore, setConfirmRestore] = useState(false)
  const [loading, setLoading] = useState(true)
  const [comparing, setComparing] = useState(false)
  const [busy, setBusy] = useState(false)
  const [dirty, setDirty] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const loadVersion = useRef(0)
  const diffVersion = useRef(0)

  const load = useCallback(async (replaceDraft = false) => {
    const version = ++loadVersion.current
    try {
      const [harness, history] = await Promise.all([
        api<HarnessStatus>('/harness'), api<{ snapshots: Snapshot[] }>('/snapshots'),
      ])
      if (version !== loadVersion.current) return
      setStatus(harness)
      setSnapshots(history.snapshots)
      if (replaceDraft) { setDraft(draftFrom(harness)); setDirty(false) }
      setError('')
    } catch (cause) {
      if (version === loadVersion.current) setError(String(cause))
    } finally {
      if (version === loadVersion.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    let active = true
    void Promise.resolve().then(() => { if (active) return load(true) })
    return () => { active = false; loadVersion.current += 1; diffVersion.current += 1 }
  }, [load])

  function field<K extends keyof Draft>(key: K, value: Draft[K]) {
    setDraft(current => current ? { ...current, [key]: value } : current)
    setDirty(true)
    setMessage('')
  }

  async function save(event: FormEvent) {
    event.preventDefault()
    if (!draft || !status) return
    setBusy(true); setError(''); setMessage('')
    try {
      const overrides: unknown = JSON.parse(draft.profile_overrides.trim() || '{}')
      if (!overrides || typeof overrides !== 'object' || Array.isArray(overrides)) throw new Error('Profile overrides must be a JSON object.')
      const settings = { ...status.settings, memory_enabled: draft.memory_enabled,
        language_context_enabled: draft.language_context_enabled, profile_overrides: overrides,
        max_cost_usd: draft.max_cost_usd.trim() === '' ? null : Number(draft.max_cost_usd) }
      for (const item of numericFields) {
        const value = Number(draft[item.key])
        if (!draft[item.key].trim() || !Number.isSafeInteger(value) || value < item.min || value > item.max) {
          throw new Error(`${item.label} must be a whole number from ${item.min} to ${item.max}.`)
        }
        settings[item.key] = value
      }
      if (settings.max_cost_usd !== null && (!Number.isFinite(settings.max_cost_usd) || settings.max_cost_usd < 0)) {
        throw new Error('Dollar cap must be a finite nonnegative amount, or blank for no dollar cap.')
      }
      await api('/harness/configure', 'POST', { settings, safety_tier: draft.safety_tier })
      setMessage('Harness settings saved.')
      await load(true)
    } catch (cause) { setError(String(cause)) }
    finally { setBusy(false) }
  }

  async function compare(snapshot: Snapshot) {
    const version = ++diffVersion.current
    setComparing(true); setComparison(null); setConfirmRestore(false); setError(''); setMessage('')
    try {
      const result = await api<SnapshotDiff>(`/snapshots/${encodeURIComponent(snapshot.id)}/diff`)
      if (version === diffVersion.current) setComparison(result)
    } catch (cause) { if (version === diffVersion.current) setError(String(cause)) }
    finally { if (version === diffVersion.current) setComparing(false) }
  }

  async function restore() {
    if (!comparison) return
    const id = comparison.id
    setBusy(true); setError(''); setMessage('')
    try {
      await api(`/snapshots/${encodeURIComponent(id)}/restore`, 'POST', {})
      setMessage('Snapshot restored. Files now match their state before the agent edit.')
      setConfirmRestore(false); setComparison(null)
      await load()
    } catch (cause) {
      setError(`Restore failed: ${String(cause)}. If files changed after the agent edit, compare and resolve those changes before restoring.`)
      setConfirmRestore(false)
    } finally { setBusy(false) }
  }

  async function checkSandbox() {
    setBusy(true); setError('')
    try {
      const result = await api<{ sandbox: HarnessStatus['sandbox'] }>('/harness/probe-sandbox', 'POST', {})
      setStatus(current => current ? { ...current, sandbox: result.sandbox } : current)
    } catch (cause) { setError(String(cause)) }
    finally { setBusy(false) }
  }

  const locked = disabled || busy || loading
  const selected = snapshots.find(item => item.id === comparison?.id)
  const budget = status?.budget
  const codeStyle = { maxHeight: 360, overflow: 'auto', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', margin: 0 } as const

  return <section className="playbook-view" aria-labelledby="harness-title">
    <ContextAccountingPanel disabled={disabled} />
    <h1 id="harness-title">Harness controls</h1>
    <p>Set task limits, inspect saved scaffolding and review file snapshots. Permissions, project scope, approvals and shared budgets remain enforced by the engine.</p>
    <nav className="settings-tabs" aria-label="Harness views">
      {(['controls', 'snapshots', 'revisions'] as const).map(value => <button key={value} type="button" className={tab === value ? 'selected' : ''} aria-pressed={tab === value} onClick={() => setTab(value)}>{value === 'controls' ? 'Controls' : value === 'snapshots' ? 'File snapshots' : 'Revision history'}</button>)}
    </nav>
    <button type="button" disabled={busy || loading} onClick={() => { setLoading(true); void load(draft === null) }}>Refresh status</button>
    {dirty && <p className="hint">Unsaved changes. Refreshing status keeps your draft.</p>}
    {error && <p className="error-text" role="alert">{error}</p>}
    {message && <p role="status">{message}</p>}
    {loading && <p role="status">Loading harness state…</p>}
    {tab === 'controls' && <>
      <ReadinessPanel readiness={status?.readiness} />
      {status && <details open>
        <summary>Current runtime</summary>
        <p>{budget ? `${budget.requests.toLocaleString()}${budget.limits?.max_requests !== undefined ? ` / ${budget.limits.max_requests.toLocaleString()}` : ''} model calls · ${budget.tokens.toLocaleString()}${budget.limits?.max_tokens !== undefined ? ` / ${budget.limits.max_tokens.toLocaleString()}` : ''} reported or reserved tokens` : 'No active task budget is available.'}</p>
        {budget && <p>{budget.cost_known ? `Tracked cost: $${budget.cost_usd.toFixed(4)}` : `Cost incomplete: $${budget.cost_usd.toFixed(4)} known; some model calls have no configured price.`}</p>}
        {budget?.limits?.max_cost_usd != null && <p className="hint">Active task dollar cap: ${budget.limits.max_cost_usd.toFixed(4)}</p>}
        <p>Sandbox: {status.sandbox.ready ? 'Available' : 'Unavailable or not yet checked'} · {status.sandbox.backend || 'No backend reported'}</p>
        <button type="button" disabled={locked} onClick={() => void checkSandbox()}>Check isolated runtime</button>
        {status.sandbox.reason && <p className="hint">{status.sandbox.reason}</p>}
        <p className="hint">Generated helpers require an available isolated runtime. Network: {status.sandbox.network || 'Not reported'}. Host files: {status.sandbox.host_filesystem || 'Not reported'}.</p>
        {status.language && <p className="hint">Language context: {status.language.adapter_count ?? 0} adapters · {status.language.languages?.filter(item => item.server_available).length ?? 0} local servers available · {status.language.languages?.filter(item => item.server_running).length ?? 0} running. {status.language.note}</p>}
      </details>}
      {draft && <form onSubmit={event => { void save(event) }} aria-label="Harness settings">
        <fieldset disabled={locked}>
          <legend>Task limits and context</legend>
          <label>Safety tier<select value={draft.safety_tier} onChange={event => field('safety_tier', event.target.value)}>{tiers.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
          <p className="hint">Tiers 4 and 5 allow automatic tool and exploit generation within the authorized scope. Every harness activation still requires a passing local evaluation. Lower tiers require human approval for generation.</p>
          <div className="form-row" style={{ flexWrap: 'wrap' }}>{numericFields.map(item => <label key={item.key}>{item.label}<input type="number" required min={item.min} max={item.max} step={1} value={draft[item.key]} onChange={event => field(item.key, event.target.value)} /></label>)}</div>
          <label>Dollar cap per task (optional)<input type="number" min={0} step="any" value={draft.max_cost_usd} onChange={event => field('max_cost_usd', event.target.value)} placeholder="No dollar cap" /></label>
          <p className="hint">A dollar cap requires configured prices for every model call. Child agents share these task limits.</p>
          <label className="scope-checkbox"><input type="checkbox" checked={draft.memory_enabled} onChange={event => field('memory_enabled', event.target.checked)} />Enable project memory</label>
          <label className="scope-checkbox"><input type="checkbox" checked={draft.language_context_enabled} onChange={event => field('language_context_enabled', event.target.checked)} />Use language context before edits</label>
          <details>
            <summary>Optional model profile overrides</summary>
            <label htmlFor="harness-profile-overrides">Profile overrides (JSON object)</label>
            <textarea id="harness-profile-overrides" rows={8} maxLength={24000} value={draft.profile_overrides} onChange={event => field('profile_overrides', event.target.value)} spellCheck={false} style={{ width: '100%', resize: 'vertical' }} />
            <p className="hint">Use instructions, preferred_tools, tool_allowlist, middleware, strategy or max_strategy_attempts. Overrides can narrow existing capabilities; unknown controls are rejected.</p>
          </details>
          <button type="submit" className="primary" disabled={!dirty}>{busy ? 'Saving…' : 'Save settings'}</button>
        </fieldset>
        {disabled && <p className="hint">Settings can be saved when the engine is idle.</p>}
      </form>}
    </>}
    {tab === 'revisions' && <>
      <p>Proposals and evaluation records are shown as read-only data. Candidate and learning text do not become trusted instructions automatically.</p>
      {!loading && !status?.evolution.length && <p>No harness proposals stored yet.</p>}
      {[...(status?.evolution || [])].reverse().map(revision => <details key={revision.revision_id}>
        <summary><strong>{revision.kind}</strong> · {revision.state} · {revision.revision_id.slice(0, 12)}</summary>
        <p className="hint">Revision: {revision.revision_id}{revision.created_ns ? ` · ${dateLabel(revision.created_ns / 1000000000)}` : ''}</p>
        <pre className="source-view" style={codeStyle}><code>{JSON.stringify(revision.payload, null, 2)}</code></pre>
        <details><summary>Validation and outcome history</summary><pre className="source-view" style={codeStyle}><code>{JSON.stringify(revision.events || [], null, 2)}</code></pre></details>
      </details>)}
    </>}
    {tab === 'snapshots' && <>
      <p>Compare a file before the agent edit with its current contents. Restore is available only while idle, and refuses to overwrite later changes.</p>
      {!loading && !snapshots.length && <p>No file snapshots stored yet.</p>}
      <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: 8 }}>
        {snapshots.map(snapshot => <button key={snapshot.id} type="button" disabled={busy} aria-pressed={comparison?.id === snapshot.id} onClick={() => { void compare(snapshot) }} style={{ textAlign: 'left', overflowWrap: 'anywhere' }}>{dateLabel(snapshot.created_at)} · {snapshot.status} · {snapshot.paths.join(', ')}</button>)}
      </div>
      {comparing && <p role="status">Loading comparison…</p>}
      {comparison && <article aria-label="Snapshot comparison">
        <h2>Snapshot {comparison.id.slice(0, 12)}</h2>
        {comparison.files.map(file => <details key={file.path} open>
          <summary>{file.path}</summary>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 16 }}>
            <div style={{ minWidth: 0 }}><h3>Before agent edit</h3><pre className="source-view" style={codeStyle}><code>{file.before || '(empty or absent)'}</code></pre></div>
            <div style={{ minWidth: 0 }}><h3>Current file</h3><pre className="source-view" style={codeStyle}><code>{file.after || '(empty or absent)'}</code></pre></div>
          </div>
        </details>)}
        <details><summary>Unified diff</summary><pre className="source-view" style={codeStyle}><code>{comparison.diff || 'No text differences.'}</code></pre></details>
        {confirmRestore ? <div>
          <p>Restore the {comparison.files.length} file(s) in this snapshot to their contents before the agent edit?</p>
          <button type="button" disabled={locked || selected?.status !== 'completed'} onClick={() => { void restore() }}>Confirm restore</button>{' '}
          <button type="button" disabled={busy} onClick={() => setConfirmRestore(false)}>Cancel</button>
        </div> : <button type="button" disabled={locked || selected?.status !== 'completed'} onClick={() => setConfirmRestore(true)}>Restore this snapshot…</button>}
        {selected?.status !== 'completed' && <p className="hint">Only completed agent edits can be restored.</p>}
      </article>}
    </>}
  </section>
}
