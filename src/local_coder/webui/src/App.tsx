import { useEffect, useMemo, useRef, useState } from 'react'
import { ArrowRight, Bug, Check, ChevronDown, Code2, Database, FileCode2, FolderOpen, GitBranch, History, LayoutPanelLeft, Plug, Plus, Search, Settings2, ShieldCheck, SlidersHorizontal, Square, X } from 'lucide-react'
import { api, running, setToken } from './api'
import type { AgentEvent, ContextPolicy, Mode, Provider, ProviderQuota, ProviderUsage, Session, Workspace } from './api'
import { ProviderSettings } from './components/ProviderSettings'
import { RedTeamScope, EngagementEvidence } from './components/RedTeamScope'
import { emptyEngagement, engagementPayload } from './redteam'
import { SecurityPlaybooks } from './components/SecurityPlaybooks'
import { MemoryInspector } from './components/MemoryInspector'
import { HarnessPanel } from './components/HarnessPanel'
import { ConnectionsPanel } from './components/ConnectionsPanel'
import { ApprovalInbox } from './components/ApprovalInbox'
import { GoalPanel, BugModels } from './components/GoalPanel'
import './App.css'
import './Workspace.css'
type Task = { task_id: string; objective: string; status?: string }
type Finding = { file?: string; line?: number; severity?: string; rule_id?: string; message?: string; description?: string }
const modeNames: Record<Mode, string> = { coding: 'Build', cybersecurity: 'Cybersecurity', bug_hunting: 'Bug hunting', red_team: 'Red Team' }
const providerIdentity = (value: Provider | null) => value ? `${value.backend}|${value.base_url}|${value.model_id}` : ''
function needsSpendingBudget(value: Provider | null) {
  if (!value) return true
  try { if (['localhost', '127.0.0.1', '[::1]'].includes(new URL(value.base_url).hostname)) return false } catch { return true }
  return value.input_cost_per_million !== 0 || value.output_cost_per_million !== 0 || (value.request_cost_usd !== undefined && value.request_cost_usd !== 0)
}
export default function App() {
  const [sessions, setSessions] = useState<Session[]>([]), [selected, setSelected] = useState(''), [events, setEvents] = useState<AgentEvent[]>([])
  const [provider, setProvider] = useState<Provider | null>(null), [workspace, setWorkspace] = useState<Workspace | null>(null)
  const [contextPolicy, setContextPolicy] = useState<ContextPolicy | null>(null), [providerUsage, setProviderUsage] = useState<ProviderUsage | null>(null), [quota, setQuota] = useState<ProviderQuota | null>(null), [preferLocal, setPreferLocal] = useState(false)
  const [project, setProject] = useState(''), [files, setFiles] = useState<string[]>([]), [filter, setFilter] = useState('')
  const [source, setSource] = useState<{ path: string; content: string } | null>(null), [diff, setDiff] = useState('')
  const [view, setView] = useState('conversation'), [mode, setMode] = useState<Mode>('coding'), [request, setRequest] = useState(''), [target, setTarget] = useState('.'), [fix, setFix] = useState(false), [action, setAction] = useState('run')
  const [goalId, setGoalId] = useState('')
  const [settings, setSettings] = useState(false), [error, setError] = useState(''), [connection, setConnection] = useState('Connecting'), [sending, setSending] = useState(false), [token, setTokenInput] = useState('')
  const [engagement, setEngagement] = useState(''), [engagementDraft, setEngagementDraft] = useState(emptyEngagement)
  const [explicitProvider, setExplicitProvider] = useState(''), [taskDollarCap, setTaskDollarCap] = useState<number | null>(null)
  const sequence = useRef(0), instance = useRef('')
  const current = sessions.find(session => session.session_id === selected), active = sessions.find(running), busy = !!active || sending
  const quotaPaused = current?.status === 'quota_exhausted'
  const quotaSelectionReady = explicitProvider !== '' && explicitProvider === providerIdentity(provider)
  const quotaBudgetReady = !needsSpendingBudget(provider) || (taskDollarCap !== null && taskDollarCap > 0)
  function chooseSession(id: string) { setView('conversation'); if (id === selected) return; sequence.current = 0; setEvents([]); setSelected(id) }
  useEffect(() => {
    let stopped = false
    let timer: ReturnType<typeof setTimeout>
    async function poll() {
      try {
        const [status, config, tree, changes, listing] = await Promise.all([api<{ project_root: string; sessions: Session[] }>('/status'), api<{ provider: Provider | null; context_policy?: ContextPolicy }>(`/settings?mode=${encodeURIComponent(mode)}`), api<Workspace>('/workspace'), api<{ diff: string }>('/diff'), api<{ files: string[] }>('/files')])
        if (stopped) return
        setSessions(status.sessions); setProject(status.project_root); setProvider(config.provider); setContextPolicy(config.context_policy || null); setWorkspace(tree); setDiff(changes.diff); setFiles(listing.files)
        if (selected) {
          const result = await api<{ events: AgentEvent[]; latest_seq: number; instance_id: string }>(`/events?session_id=${encodeURIComponent(selected)}&since=${sequence.current}`)
          if (stopped) return
          if (instance.current && instance.current !== result.instance_id) { sequence.current = 0; setEvents([]) }
          else { sequence.current = result.latest_seq; setEvents(previous => [...previous, ...result.events].slice(-600)) }
          instance.current = result.instance_id
        }
        setConnection('Connected')
      } catch (error) { if (!stopped) { setConnection('Disconnected'); setError(error instanceof Error ? error.message : 'Cannot connect to the local engine') } }
      finally { if (!stopped) timer = setTimeout(poll, 1800) }
    }
    void poll()
    return () => { stopped = true; clearTimeout(timer) }
  }, [selected, token, mode])
  useEffect(() => {
    let stopped = false
    let timer: ReturnType<typeof setTimeout>
    const controller = new AbortController()
    async function pollQuota() {
      try {
        const result = await api<ProviderQuota>('/providers/quota', 'GET', undefined, controller.signal)
        if (!stopped) setQuota(result)
      } catch { /* Quota is unknown while the engine is unavailable. */ }
      finally { if (!stopped) timer = setTimeout(() => { void pollQuota() }, 1800) }
    }
    void pollQuota()
    return () => { stopped = true; controller.abort(); clearTimeout(timer) }
  }, [token])
  useEffect(() => {
    let stopped = false
    let timer: ReturnType<typeof setTimeout>
    const controller = new AbortController()
    let authenticatedRouter = false
    try { authenticatedRouter = new URL(provider?.base_url || '').hostname === 'openrouter.ai' && provider?.key_set === true } catch { /* Unconfigured endpoint. */ }
    async function pollUsage() {
      if (!authenticatedRouter) { if (!stopped) setProviderUsage(null); return }
      try {
        const result = await api<ProviderUsage>('/providers/usage', 'GET', undefined, controller.signal)
        if (!stopped) setProviderUsage(result)
      } catch (cause) {
        if (!stopped) setProviderUsage({ available: false, free_model_daily_requests: null, error: cause instanceof Error ? cause.message : 'Account request allowance unavailable' })
      } finally { if (!stopped) timer = setTimeout(() => { void pollUsage() }, 30000) }
    }
    void pollUsage()
    return () => { stopped = true; controller.abort(); clearTimeout(timer) }
  }, [provider?.base_url, provider?.key_set, provider?.model_id, active?.session_id, active?.status, token])
  useEffect(() => {
    let stopped = false
    let timer: ReturnType<typeof setTimeout>
    const controller = new AbortController()
    async function loadBudget() {
      try {
        const result = await api<{ settings: { max_cost_usd?: number | null } }>('/harness', 'GET', undefined, controller.signal)
        if (!stopped) setTaskDollarCap(result.settings.max_cost_usd ?? null)
      } catch { if (!stopped) setTaskDollarCap(null) }
      finally { if (!stopped) timer = setTimeout(() => { void loadBudget() }, 5000) }
    }
    void loadBudget()
    return () => { stopped = true; controller.abort(); clearTimeout(timer) }
  }, [token, view])
  const tasks = useMemo(() => { const result = new Map<string, Task>(); events.forEach(event => { if (event.event_type === 'plan_created' && Array.isArray(event.data.tasks)) (event.data.tasks as Task[]).forEach(task => result.set(task.task_id, task)); if (event.event_type === 'task_status') { const id = String(event.data.task_id), task = result.get(id); if (task) result.set(id, { ...task, status: String(event.data.status) }) } }); return [...result.values()] }, [events])
  const findings = useMemo(() => { const result = new Map<string, Finding>(); events.forEach(event => { if (event.event_type === 'scan_completed' && Array.isArray(event.data.findings)) (event.data.findings as Finding[]).filter(f => f.file).forEach(f => result.set(`${f.file}:${f.line}:${f.rule_id || f.description}`, f)) }); return [...result.values()] }, [events])
  async function openFile(path: string) { try { setSource(await api(`/file?path=${encodeURIComponent(path)}`)); setView('workspace') } catch (error) { setError(error instanceof Error ? error.message : 'Cannot open file') } }
  async function submitTask(objective: string, taskMode: Mode, sourceOnly = false) {
    if (busy || quota?.blocked || !objective.trim()) return
    if (!provider) { setSettings(true); return }
    setSending(true); setError('')
    try {
      let engagementId = sourceOnly ? '' : engagement
      if (taskMode === 'red_team' && !engagementId && !sourceOnly) {
        const created = await api<{ id: string }>('/engagements', 'POST', engagementPayload(engagementDraft, objective))
        engagementId = created.id; setEngagement(created.id)
      }
      const job = await api<Session>('/jobs', 'POST', {
        goal_id: goalId || undefined, request: objective, mode: taskMode, action: taskMode === 'red_team' ? 'run' : action,
        target_path: sourceOnly ? engagementDraft.paths.split('\n').find(path => path.trim()) || '.' : target || '.',
        fix: taskMode === 'red_team' ? false : fix,
        engagement_id: taskMode === 'red_team' ? engagementId || undefined : undefined,
        parent_session_id: sourceOnly ? undefined : taskMode === 'red_team' ? (current?.engagement_id === engagementId ? selected : undefined) : selected || undefined,
      })
      setSessions(previous => [job, ...previous]); chooseSession(job.session_id); setRequest('')
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Cannot start task') }
    finally { setSending(false) }
  }
  async function start(event: React.FormEvent) { event.preventDefault(); await submitTask(request, mode) }
  async function reviewProject() {
    const objective = request.trim() || 'Review this local project source for security weaknesses. Report evidence-linked findings; keep HTTP observations off.'
    setMode('red_team'); setAction('run'); setRequest(objective)
    if (!engagement) setEngagementDraft(previous => ({ ...previous, active_http: false, origins: '', excluded_urls: '', authorization: 'The operator requested a local source-code security review of this project.' }))
    await submitTask(objective, 'red_team', !engagement)
  }
  async function resume() {
    if (!current?.checkpoint || busy || quota?.blocked) return
    if (quotaPaused && !quotaSelectionReady) { setSettings(true); setError('Choose and save the model you want to use before resuming.'); return }
    if (quotaPaused && !quotaBudgetReady) { setView('harness'); setError('Set a positive task dollar cap before continuing with a metered or unpriced cloud model.'); return }
    setSending(true)
    try {
      const job = await api<Session>('/jobs', 'POST', { request: `Resume: ${current.request}`, resume_checkpoint: current.checkpoint, mode: current.mode || mode, engagement_id: current.engagement_id, allow_model_change: quotaPaused ? true : undefined })
      setSessions(previous => [job, ...previous]); chooseSession(job.session_id)
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Cannot resume checkpoint') }
    finally { setSending(false) }
  }
  async function stop() { if (active) try { await api(`/jobs/${active.session_id}/cancel`, 'POST', {}) } catch (error) { setError(error instanceof Error ? error.message : 'Cannot stop task') } }
  async function openIDE() { try { await window.localCoder?.openIDE() } catch (error) { setError(error instanceof Error ? error.message : 'Cannot open the IDE') } }
  const projectName = project.split(/[\\/]/).filter(Boolean).pop() || 'No project'
  return <div className="manager">
    <aside className="manager-sidebar"><div className="sidebar-top"><button className="brand" aria-label="Local Coder home" onClick={() => chooseSession('')}><span className="brand-mark">Λ</span></button><span className="sidebar-caption">Local Coder</span><button className="icon-button" aria-label="Conversation history" onClick={() => setView('history')}><History size={15} /></button></div>
      <nav className="main-nav" aria-label="Main navigation"><button className={view === "goals" ? "selected" : ""} onClick={() => setView("goals")}><GitBranch size={15} />Goals & milestones</button><button className={view === 'conversation' && !selected ? 'selected' : ''} onClick={() => chooseSession('')}><Plus size={15} />New conversation</button><button className={view === 'history' ? 'selected' : ''} onClick={() => setView('history')}><History size={14} />Conversation history</button><button className={view === 'workspace' ? 'selected' : ''} onClick={() => setView('workspace')}><GitBranch size={14} />Workspace changes<span className="count">{workspace?.files.length || 0}</span></button><button className={view === 'playbooks' ? 'selected' : ''} onClick={() => setView('playbooks')}><ShieldCheck size={14} />Security playbooks</button><button className={view === 'memory' ? 'selected' : ''} onClick={() => setView('memory')}><Database size={14} />Project memory</button><button className={view === 'harness' ? 'selected' : ''} onClick={() => setView('harness')}><SlidersHorizontal size={14} />Harness controls</button><button className={view === 'connections' ? 'selected' : ''} onClick={() => setView('connections')}><Plug size={14} />Connections & skills</button><button onClick={() => setSettings(true)}><Settings2 size={14} />Customizations</button></nav>
      <div className="sidebar-section-heading"><span>Projects</span><button className="icon-button" aria-label="Open project folder" title={window.localCoder ? 'Open project folder' : 'Choose a project when starting the engine'} disabled={!window.localCoder || busy} onClick={() => { void window.localCoder?.chooseProject().catch(error => setError(String(error))) }}><FolderOpen size={14} /></button></div><button className="project-row" title={project} onClick={() => setView('workspace')}><FolderOpen size={14} /><span>{projectName}</span></button>
      <div className="sidebar-section-heading conversation-heading"><span>Conversations</span><button className="icon-button" aria-label="New conversation" onClick={() => chooseSession('')}><Plus size={14} /></button></div><div className="sidebar-conversations">{sessions.map(session => <button key={session.session_id} className={selected === session.session_id ? 'selected' : ''} onClick={() => chooseSession(session.session_id)} title={session.request}><span className={`status-dot ${session.status || ''}`} /><span>{session.request || session.phase}</span></button>)}{!sessions.length && <p>No conversations yet</p>}</div>
      <div className="sidebar-bottom"><button className="ide-launch" disabled={!window.localCoder} title={window.localCoder ? 'Open the Local Coder Code OSS fork' : 'Launch the Code OSS fork from the desktop app'} onClick={() => { void openIDE() }}><Code2 size={15} />Open IDE<ArrowRight size={13} /></button><button onClick={() => setSettings(true)}><Settings2 size={14} />Settings</button></div>
    </aside>
    <main className={`manager-main ${view === 'conversation' && !current ? 'empty-conversation' : ''}`}><header className="main-topbar"><span>{view === 'goals' ? 'Goals & milestones' : view === 'history' ? 'Conversation history' : view === 'workspace' ? projectName : view === 'memory' ? 'Project memory' : view === 'harness' ? 'Harness controls' : view === 'connections' ? 'Connections & skills' : view === 'playbooks' ? 'Security playbooks' : current?.request || ''}</span><div><span className={`connection ${connection.toLowerCase()}`}><span className="status-dot" />{connection}</span>{active && <button className="active-task" onClick={() => chooseSession(active.session_id)}>Task running</button>}<button className="icon-button" aria-label="Open IDE" disabled={!window.localCoder} onClick={() => { void openIDE() }}><LayoutPanelLeft size={16} /></button></div></header>
      {view === "goals" ? <GoalPanel selected={goalId} onSelect={id => { setGoalId(id); setView("conversation") }} /> : view === 'memory' ? <MemoryInspector /> : view === 'harness' ? <HarnessPanel disabled={busy} /> : view === 'connections' ? <ConnectionsPanel /> : view === 'playbooks' ? <SecurityPlaybooks /> : view === 'history' ? <section className="history-view"><h1>Conversation history</h1><label className="history-search"><Search size={16} /><input aria-label="Search conversations" value={filter} onChange={event => setFilter(event.target.value)} placeholder="Search conversations" /></label>{sessions.filter(session => session.request?.toLowerCase().includes(filter.toLowerCase())).map(session => <button key={session.session_id} onClick={() => chooseSession(session.session_id)}><span>{session.request}</span><small>{session.status || session.phase}</small><ArrowRight size={14} /></button>)}</section> : view === 'workspace' ? <section className="workspace-review"><div className="review-files"><h2>Files & changes</h2><label className="file-search"><Search size={13} /><input aria-label="Filter files" value={filter} placeholder="Find a file" onChange={event => setFilter(event.target.value)} /></label><button className="view-diff" onClick={() => setSource(null)}><GitBranch size={14} />View changes <span className="count">{workspace?.files.length || 0}</span></button>{files.filter(file => file.toLowerCase().includes(filter.toLowerCase())).map(file => <button key={file} title={file} onClick={() => { void openFile(file) }}><FileCode2 size={13} /><span>{file}</span></button>)}</div><div className="review-source"><header><span>{source?.path || 'Workspace diff'}</span><span className="branch-label"><GitBranch size={12} />{workspace?.branch}</span></header>{source ? <div className="source-view">{source.content.split('\n').map((line, index) => <div className="source-line" key={index}><span>{index + 1}</span><code>{line || ' '}</code></div>)}</div> : <><div className="changed-files">{workspace?.files.map(file => <button key={file.path} onClick={() => { void openFile(file.path) }}>{file.path}<span className="additions">+{file.additions}</span><span className="deletions">−{file.deletions}</span></button>)}</div><pre className="diff-view">{diff ? diff.split('\n').map((line, index) => <div key={index} className={line.startsWith('+') ? 'diff-add' : line.startsWith('-') ? 'diff-remove' : ''}>{line || ' '}</div>) : 'No tracked changes. Select a new file above to inspect it.'}</pre></>}</div></section> : <>
      <div className="conversation-content">{quotaPaused && <aside className="quota-resume-steps"><strong>Your saved step is ready to continue</strong><p>Choose a model explicitly, set a positive task dollar cap for metered cloud use, then select Resume saved step.</p><div><button type="button" onClick={() => { setPreferLocal(false); setSettings(true) }}>{quotaSelectionReady ? 'Model selected · change' : 'Choose continuation model'}</button><button type="button" onClick={() => setView('harness')}>{taskDollarCap !== null && taskDollarCap > 0 ? `Task dollar cap: $${taskDollarCap}` : 'Set task dollar cap'}</button></div>{!quotaBudgetReady && <p className="hint">A positive dollar cap is required for a paid or unpriced cloud continuation. On-device models do not require a paid API budget.</p>}</aside>}{current && <><div className="user-message"><small>You</small><p>{current.request}</p></div><article className="agent-response"><header><span className="brand-mark">Λ</span><strong>Local Coder</strong><small className={`task-state ${current.status}`}>{current.status?.replaceAll('_', ' ') || 'Saved'}</small></header>{current.checkpoint && ['cancelled', 'interrupted', 'quota_exhausted'].includes(current.status || '') && <button className="resume-button" disabled={busy || quota?.blocked || (quotaPaused && (!quotaSelectionReady || !quotaBudgetReady))} title={quota?.blocked ? 'Choose an available model in settings before resuming' : undefined} onClick={() => { void resume() }}>Resume saved step</button>}{tasks.length > 0 && <div className="task-checklist">{tasks.map(task => <div key={task.task_id}>{task.status === 'completed' ? <Check size={14} /> : <span className={`status-dot ${task.status || ''}`} />}<span>{task.objective}</span></div>)}</div>}{current.result ? <pre className="report">{current.result}</pre> : current.error ? <p role="alert" className="error-text">{current.error}</p> : <p className="hint">{running(current) ? 'Working in your workspace. Activity appears below.' : 'No report was saved for this task.'}</p>}{current.engagement_id && <EngagementEvidence id={current.engagement_id} status={current.status} />}{findings.length > 0 && <details className="candidates"><summary>{findings.length} static candidates · unverified</summary>{findings.map((finding, index) => <div key={index}><span className="severity">{finding.severity}</span><p>{finding.message || finding.description}</p><button onClick={() => { if (finding.file) void openFile(finding.file) }}>{finding.file}:{finding.line}</button></div>)}</details>}<details className="activity-log" open={running(current)}><summary>Activity · {events.length} events</summary>{events.map(event => <div key={event.seq}><small>{event.source}</small><p>{event.message}</p></div>)}</details></article></>}</div>
      <div className="composer-region">{goalId && <div className="goal-attached">Persistent goal attached <button onClick={() => setView("goals")}>View</button><button onClick={() => setGoalId("")}>Detach</button></div>}{mode === "bug_hunting" && <BugModels disabled={busy} />}{(contextPolicy || providerUsage) && <div className="provider-context-status"><span title={`Prompt budgets use conservative UTF-8 byte bounds rather than exact tokenizer counts. The model window is its detected token limit. Source: ${contextPolicy?.source || 'Not reported'}`}>{contextPolicy ? `${modeNames[mode]} · ${contextPolicy.model_limit?.toLocaleString() ?? '?'} token window · ${contextPolicy.prompt_tokens?.toLocaleString() ?? '?'} prompt allowance / ${contextPolicy.completion_tokens?.toLocaleString() ?? '?'} output tokens` : 'Context allocation unavailable'}</span>{providerUsage && <button type="button" className={providerUsage.free_model_daily_requests && providerUsage.free_model_daily_requests.remaining <= 5 ? 'quota-low' : ''} onClick={() => setSettings(true)}>{providerUsage.free_model_daily_requests ? `Free requests: ${providerUsage.free_model_daily_requests.remaining} / ${providerUsage.free_model_daily_requests.limit} remaining today` : 'Free request allowance unknown'}</button>}</div>}<div className="project-selector"><button title={project} disabled={!window.localCoder || busy} onClick={() => { void window.localCoder?.chooseProject().catch(error => setError(String(error))) }}>{projectName}<ChevronDown size={12} /></button></div><form className="composer" onSubmit={start}><textarea id="task-input" aria-label="Task request" placeholder="Ask anything, @ to mention a file, / for actions" value={request} onChange={event => setRequest(event.target.value)} onKeyDown={event => { if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') event.currentTarget.form?.requestSubmit() }} rows={2} />{mode === 'red_team' && <RedTeamScope draft={engagementDraft} setDraft={setEngagementDraft} selected={engagement} select={setEngagement} disabled={busy || quota?.blocked || connection !== 'Connected'} onReviewProject={() => { void reviewProject() }} />}{mode !== 'coding' && mode !== 'red_team' && <div className="investigation-options"><label>Scope<input aria-label="Investigation target" value={target} onChange={event => setTarget(event.target.value)} /></label><label><input type="checkbox" checked={fix} onChange={event => setFix(event.target.checked)} />Apply fixes & run tests</label></div>}<div className="composer-bottom"><button type="button" className="icon-button" aria-label="Provider settings" onClick={() => setSettings(true)}><Plus size={15} /></button><button type="button" className="model-button" onClick={() => setSettings(true)}>{provider?.model_id || 'Connect a model'}<ChevronDown size={11} /></button><div className="composer-spacer" /><label className="mode-select">{mode === 'cybersecurity' ? <ShieldCheck size={13} /> : mode === 'bug_hunting' ? <Bug size={13} /> : <Code2 size={13} />}<select aria-label="Operating mode" value={mode} onChange={event => { setMode(event.target.value as Mode); if (event.target.value === 'red_team') setAction('run') }}>{Object.entries(modeNames).map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></label><select className="action-select" disabled={mode === 'red_team'} aria-label="Task action" value={action} onChange={event => setAction(event.target.value)}><option value="run">Run</option><option value="plan">Plan</option><option value="review">Review</option></select>{active ? <button className="send-button stop-button" type="button" aria-label="Stop task" onClick={() => { void stop() }}><Square size={13} /></button> : <button className="send-button" type="submit" aria-label="Start task" disabled={busy || quota?.blocked || !request.trim() || connection !== 'Connected'}><ArrowRight size={16} /></button>}</div></form>{mode !== 'coding' && <p className="composer-caption">{mode === 'red_team' ? 'Scoped engagement · evidence journal · bounded observations' : fix ? 'Local reproduction and remediation enabled' : 'Read-only review · candidates require verification'}</p>}</div></>}
      {quota?.blocked && <aside className="provider-quota-notice" role="alert"><div><strong>Model quota reached</strong><p>{quota.reason || 'The selected provider cannot accept another request.'}</p><p className="hint">The daily free allowance is shared across OpenRouter free models. Wait for reset, use an on-device model, or explicitly select a metered model.</p>{quota.reset_at && <p className="hint">Reset: {new Date(typeof quota.reset_at === 'number' ? quota.reset_at * 1000 : quota.reset_at).toLocaleString()}</p>}{quota.model_id && <code>{quota.model_id}</code>}</div><div><button type="button" onClick={() => { setPreferLocal(true); setSettings(true) }}>Use an on-device model</button><button type="button" onClick={() => { setPreferLocal(false); setSettings(true) }}>Choose a model in settings</button></div></aside>}
      <ApprovalInbox connected={connection === 'Connected'} />
      {error && <div className="error-banner" role="alert"><span>{error}</span><button aria-label="Dismiss error" className="icon-button" onClick={() => setError('')}><X size={14} /></button></div>}{!window.localCoder && connection !== 'Connected' && <label className="token-entry">Server access token<input aria-label="Server access token" type="password" autoComplete="off" value={token} placeholder="For a token-protected engine" onChange={event => { setToken(event.target.value); setTokenInput(event.target.value) }} /></label>}
    </main>{settings && <ProviderSettings provider={provider} mode={mode} usage={providerUsage} contextPolicy={contextPolicy} preferLocal={preferLocal} close={() => { setSettings(false); setPreferLocal(false) }} saved={value => { setProvider(value); setExplicitProvider(providerIdentity(value)); setError('') }} />}
  </div>
}
