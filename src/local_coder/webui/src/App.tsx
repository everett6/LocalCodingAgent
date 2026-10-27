import { useEffect, useMemo, useRef, useState } from 'react'
import { ArrowRight, Bug, Check, ChevronDown, Code2, Database, FileCode2, FolderOpen, GitBranch, History, Plug, Plus, Search, Settings2, ShieldCheck, SlidersHorizontal, Square, X, ListTodo, PanelLeft, PanelRight, Paperclip, ChevronRight, Pin, Sparkles } from 'lucide-react'
import { api, running, setToken } from './api'
import type { AgentEvent, ContextPolicy, Mode, Provider, ProviderQuota, ProviderUsage, Session, Workspace } from './api'
import { ProviderSettings } from './components/ProviderSettings'
import { RedTeamScope, EngagementEvidence } from './components/RedTeamScope'
import { emptyEngagement, engagementPayload } from './redteam'
import { SecurityPlaybooks } from './components/SecurityPlaybooks'
import { MemoryInspector } from './components/MemoryInspector'
import { HarnessPanel } from './components/HarnessPanel'
import { ConnectionsPanel } from './components/ConnectionsPanel'
import { ResearchWorkbench } from './components/ResearchWorkbench'
import { FindingsWorkbench } from './components/FindingsWorkbench'
import { EvaluationPanel } from './components/EvaluationPanel'
import { DoctorPanel } from './components/DoctorPanel'
import { ApprovalInbox } from './components/ApprovalInbox'
import { GoalPanel, BugModels } from './components/GoalPanel'
import './App.css'
import './Workspace.css'
import './Workflows.css'
import './Studio.css'
import './components/LocalModelStudio.css'
import './Typography.css'
import { PRODUCT } from './product'
import { matchingSlashCommands, parseSlashCommand, slashCommands } from './slashCommands'
import type { SlashName } from './slashCommands'
import { CommandPalette } from './components/CommandPalette'
import type { Command as PaletteCommand } from './workspaceCommands'
import { TaskInbox } from './components/TaskInbox'
import { ReportView } from './components/ReportView'
import { RunInspector } from './components/RunInspector'
import { isContextPath, taskObjective, planDraft, validPlanTasks, workspaceNeeds, stateLabel } from './workspaceUI'
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
  const [detail, setDetail] = useState<Session | null>(null), [sessionsTruncated, setSessionsTruncated] = useState(false)
  const [sessions, setSessions] = useState<Session[]>([]), [selected, setSelected] = useState(''), [events, setEvents] = useState<AgentEvent[]>([])
  const [provider, setProvider] = useState<Provider | null>(null), [workspace, setWorkspace] = useState<Workspace | null>(null)
  const [contextPolicy, setContextPolicy] = useState<ContextPolicy | null>(null), [providerUsage, setProviderUsage] = useState<ProviderUsage | null>(null), [quota, setQuota] = useState<ProviderQuota | null>(null), [preferLocal, setPreferLocal] = useState(false)
  const [project, setProject] = useState(''), [files, setFiles] = useState<string[]>([]), [filter, setFilter] = useState('')
  const [workspaceRefresh, setWorkspaceRefresh] = useState(0)
  const [source, setSource] = useState<{ path: string; content: string } | null>(null), [diff, setDiff] = useState('')
  const [view, setView] = useState('conversation'), [mode, setMode] = useState<Mode>('coding'), [request, setRequest] = useState(''), [target, setTarget] = useState('.'), [fix, setFix] = useState(false), [action, setAction] = useState('run')
  const [goalId, setGoalId] = useState('')
  const [settings, setSettings] = useState(false), [error, setError] = useState(''), [connection, setConnection] = useState('Connecting'), [sending, setSending] = useState(false), [token, setTokenInput] = useState('')
  const [engagement, setEngagement] = useState(''), [engagementDraft, setEngagementDraft] = useState(emptyEngagement)
  const [explicitProvider, setExplicitProvider] = useState(''), [taskDollarCap, setTaskDollarCap] = useState<number | null>(null)
  const [palette, setPalette] = useState<'commands' | 'files' | 'context' | null>(null)
  const [pinsByProject, setPinsByProject] = useState<Record<string, string[]>>({}), [toolsOpen, setToolsOpen] = useState(false)
  const pinned = useMemo(() => {
    if (!project) return []
    if (pinsByProject[project]) return pinsByProject[project]
    try { const stored: unknown = JSON.parse(localStorage.getItem(`workspace-pins:${project}`) || '[]'); return Array.isArray(stored) ? stored.filter((id): id is string => typeof id === 'string').slice(0,200) : [] } catch { return [] }
  }, [project, pinsByProject])
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false), [inspectorOpen, setInspectorOpen] = useState(true)
  const [contextFiles, setContextFiles] = useState<string[]>([])
  const [commandNotice, setCommandNotice] = useState(''), [slashSelected, setSlashSelected] = useState(0), [slashHidden, setSlashHidden] = useState(false)
  const slashOptions = slashHidden ? [] : matchingSlashCommands(request)
  const slashIndex = Math.min(slashSelected, Math.max(0, slashOptions.length - 1))
  const composerRef = useRef<HTMLTextAreaElement>(null)
  useEffect(() => { if (slashOptions.length) document.getElementById(`composer-slash-${slashIndex}`)?.scrollIntoView({ block: 'nearest' }) }, [slashIndex, slashOptions.length])
  const sequence = useRef(0), instance = useRef('')
  const summary = sessions.find(session => session.session_id === selected)
  const current = detail?.session_id === selected ? { ...detail, ...summary, request: detail.request, recovery: detail.recovery?.kind === 'reconciliation_required' ? detail.recovery : summary?.recovery || detail.recovery } : summary
  const active = sessions.find(running), busy = !!active || sending
  const quotaPaused = current?.status === 'quota_exhausted'
  const quotaSelectionReady = explicitProvider !== '' && explicitProvider === providerIdentity(provider)
  const quotaBudgetReady = !needsSpendingBudget(provider) || (taskDollarCap !== null && taskDollarCap > 0)
  function chooseSession(id: string) {
    setView('conversation')
    const session = sessions.find(item => item.session_id === id)
    if (session) setGoalId(session.goal_id || '')
    if (session?.mode) setMode(session.mode)
    if (session?.engagement_id) setEngagement(session.engagement_id)
    if (id === selected) return
    sequence.current = 0; setEvents([]); setDetail(null); setSelected(id)
  }
  function newTask() { chooseSession(''); setCommandNotice(''); setRequest(''); setContextFiles([]); setGoalId(''); setMode('coding'); setAction('run'); requestAnimationFrame(() => composerRef.current?.focus()) }
  useEffect(() => {
    if (!project || !window.localCoder?.loadTaskPins) return
    let cancelled = false
    void window.localCoder.loadTaskPins().then(ids => {
      if (!cancelled) setPinsByProject(previous => previous[project] ? previous : {...previous, [project]: ids})
    }).catch(() => { /* Browser storage remains available if native preferences cannot be read. */ })
    return () => { cancelled = true }
  }, [project])
  function togglePin(id: string) {
    if (!project) return
    const next = pinned.includes(id) ? pinned.filter(value => value !== id) : [...pinned, id].slice(-200)
    setPinsByProject(previous => ({...previous, [project]: next}))
    if (window.localCoder?.saveTaskPins) void window.localCoder.saveTaskPins(next).catch(() => setError('Task pins could not be saved. They remain available until this window closes.'))
    else try { localStorage.setItem(`workspace-pins:${project}`, JSON.stringify(next)) } catch { /* Navigation remains available without storage. */ }
  }
  useEffect(() => {
    function shortcut(event: KeyboardEvent) {
      if (settings || !(event.metaKey || event.ctrlKey) || event.altKey) return
      const key = event.key.toLowerCase()
      if (key === 'k') { event.preventDefault(); setPalette(previous => previous ? null : 'commands') }
      if (key === 'p') { event.preventDefault(); setPalette('files') }
      if (key === 'b') { event.preventDefault(); setSidebarCollapsed(previous => !previous) }
    }
    window.addEventListener('keydown', shortcut)
    return () => window.removeEventListener('keydown', shortcut)
  }, [settings])
  useEffect(() => {
    let stopped = false
    let timer: ReturnType<typeof setTimeout>
    let detailStamp = ''
    async function poll() {
      try {
        const [status, config] = await Promise.all([api<{ project_root: string; sessions: Session[]; truncated?: boolean }>('/status?summary=1'), api<{ provider: Provider | null; context_policy?: ContextPolicy }>(`/settings?mode=${encodeURIComponent(mode)}`)])
        if (stopped) return
        setSessions(status.sessions); setSessionsTruncated(status.truncated === true); setProject(status.project_root); setProvider(config.provider); setContextPolicy(config.context_policy || null)
        const selectedSummary = status.sessions.find(item => item.session_id === selected)
        if (selectedSummary && (!detailStamp || detailStamp !== selectedSummary.updated_at)) {
          const record = await api<Session>(`/jobs/${encodeURIComponent(selected)}`)
          if (stopped) return
          setDetail(record); detailStamp = selectedSummary.updated_at || 'loaded'
          if (!running(selectedSummary)) setEvents(record.events || [])
        }
        if (selectedSummary && running(selectedSummary)) {
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
    const needs = workspaceNeeds(view, palette, source !== null)
    if (!needs.files && !needs.diff) return
    let stopped = false
    async function load() {
      try {
        if (needs.files) {
          const listing = await api<{ files: string[] }>('/files')
          if (!stopped) setFiles(listing.files)
        }
        if (needs.diff) {
          const [tree, changes] = await Promise.all([api<Workspace>('/workspace'), api<{ diff: string }>('/diff')])
          if (!stopped) { setWorkspace(tree); setDiff(changes.diff) }
        }
      } catch (cause) { if (!stopped) setError(cause instanceof Error ? cause.message : 'Cannot load workspace') }
    }
    void load()
    return () => { stopped = true }
  }, [view, palette, token, source, workspaceRefresh])
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
  const tasks = useMemo(() => { const result = new Map<string, Task>(); events.forEach(event => { if (event.event_type === 'plan_created' && Array.isArray(event.data.tasks)) validPlanTasks(event.data.tasks).forEach(task => result.set(task.task_id, task)); if (event.event_type === 'task_status') { const id = String(event.data.task_id), task = result.get(id); if (task) result.set(id, { ...task, status: String(event.data.status) }) } }); return [...result.values()] }, [events])
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
        goal_id: goalId || null, request: taskObjective(objective, contextFiles, taskMode), mode: taskMode, action: taskMode === 'red_team' ? 'run' : action,
        target_path: sourceOnly ? engagementDraft.paths.split('\n').find(path => path.trim()) || '.' : target || '.',
        fix: taskMode === 'red_team' ? false : fix,
        engagement_id: taskMode === 'red_team' ? engagementId || undefined : undefined,
        parent_session_id: sourceOnly ? undefined : taskMode === 'red_team' ? (current?.engagement_id === engagementId ? selected : undefined) : selected || undefined,
      })
      setSessions(previous => [job, ...previous]); chooseSession(job.session_id); setRequest(''); setContextFiles([])
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Cannot start task') }
    finally { setSending(false) }
  }
  async function start(event: React.FormEvent) {
    event.preventDefault()
    if (request.trim() === '/') { await runSlash('help'); return }
    const command = parseSlashCommand(request)
    if (command) {
      if (!command.name) { setCommandNotice('Unknown command. Type / to see available commands.'); return }
      await runSlash(command.name, command.argument); return
    }
    await submitTask(request, mode)
  }
  async function reviewProject() {
    const objective = request.trim() || 'Review this local project source for security weaknesses. Report evidence-linked findings; keep HTTP observations off.'
    setMode('red_team'); setAction('run'); setRequest(objective)
    if (!engagement) setEngagementDraft(previous => ({ ...previous, active_http: false, origins: '', excluded_urls: '', authorization: 'The operator requested a local source-code security review of this project.' }))
    await submitTask(objective, 'red_team', !engagement)
  }
  async function resume() {
    if (!current?.checkpoint || busy || quota?.blocked || current.recovery?.kind === 'reconciliation_required') return
    if (quotaPaused && !quotaSelectionReady) { setSettings(true); setError('Choose and save the model you want to use before resuming.'); return }
    if (quotaPaused && !quotaBudgetReady) { setView('harness'); setError('Set a positive task dollar cap before continuing with a metered or unpriced cloud model.'); return }
    setSending(true)
    try {
      const job = await api<Session>('/jobs', 'POST', { request: `Resume: ${current.request}`, resume_checkpoint: current.checkpoint, mode: current.mode || mode, engagement_id: current.engagement_id, goal_id: current.goal_id, parent_session_id: current.session_id, allow_model_change: quotaPaused ? true : undefined })
      setSessions(previous => [job, ...previous]); chooseSession(job.session_id)
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Cannot resume checkpoint') }
    finally { setSending(false) }
  }
  async function stop() { if (!active) return false; try { await api(`/jobs/${active.session_id}/cancel`, 'POST', {}); return true } catch (error) { setError(error instanceof Error ? error.message : 'Cannot stop task'); return false } }
  async function runSlash(name: SlashName, argument = '') {
    const command = slashCommands.find(item => item.name === name)
    if (argument && !(command && 'acceptsText' in command && command.acceptsText)) {
      setCommandNotice(`/${name} does not take arguments. Type /help for command descriptions.`); return
    }
    setRequest(''); setSlashHidden(true); setCommandNotice('')
    if (name === 'status') {
      setCommandNotice('Checking engine status…')
      try {
        const fresh = await api<{ sessions: Session[] }>('/status?summary=1')
        const task = fresh.sessions.find(running)
        setCommandNotice(`Engine connected · ${provider?.model_id || 'No model selected'}\n${task ? `Active task: ${task.request || task.session_id}` : 'No active task'}\n${modeNames[mode]} · ${contextPolicy?.model_limit?.toLocaleString() || 'Unknown'} context tokens`)
      } catch (cause) { setCommandNotice(`Status unavailable: ${cause instanceof Error ? cause.message : 'Engine not reachable'}`) }
    } else if (name === 'help') setCommandNotice(slashCommands.map(item => `/${item.name} — ${item.description}`).join('\n'))
    else if (name === 'model' || name === 'local') { setPreferLocal(name === 'local'); setSettings(true) }
    else if (name === 'context') navigate('harness')
    else if (name === 'memory') navigate('memory')
    else if (name === 'tasks') navigate('tasks')
    else if (name === 'clear' || name === 'new') { newTask(); setCommandNotice('New conversation. Saved task history is kept.') }
    else if (name === 'stop') {
      if (active) { const cancelled = await stop(); setCommandNotice(cancelled ? 'Cancellation requested. Check the task status before starting more work.' : 'Cancellation could not be requested. Check the error and task status.') }
      else setCommandNotice('There is no active task to stop.')
    } else {
      setAction(name === 'plan' ? 'plan' : name === 'review' ? 'review' : 'run')
      if (name !== 'run') setMode('coding')
      setRequest(argument)
      setCommandNotice(`${name === 'plan' ? 'Planning' : name === 'review' ? 'Review' : 'Run'} mode selected. ${argument ? 'Your prompt is ready to submit.' : 'Describe the task below.'}`)
    }
    requestAnimationFrame(() => composerRef.current?.focus())
  }
  async function openIDE() { try { await window.localCoder?.openIDE() } catch (error) { setError(error instanceof Error ? error.message : 'Cannot open the IDE') } }
  const projectName = project.split(/[\\/]/).filter(Boolean).pop() || 'No project'
  const workbenchViews = [
    { id: 'goals', label: 'Goals & milestones', icon: GitBranch },
    { id: 'findings', label: 'Findings & verification', icon: Bug },
    { id: 'research', label: 'Research workspace', icon: Search },
    { id: 'memory', label: 'Project memory', icon: Database },
    { id: 'playbooks', label: 'Security playbooks', icon: ShieldCheck },
    { id: 'evaluations', label: 'Harness evaluations', icon: Check },
    { id: 'harness', label: 'Harness controls', icon: SlidersHorizontal },
    { id: 'connections', label: 'Connections & skills', icon: Plug },
    { id: 'doctor', label: 'Setup doctor', icon: Settings2 },
  ]
  function navigate(next: string) { setView(next); if (workbenchViews.some(item => item.id === next)) setToolsOpen(true) }
  function prepare(title: string, nextMode: Mode = 'coding', nextAction = 'run') { setRequest(title); setMode(nextMode); setAction(nextAction); chooseSession(''); requestAnimationFrame(() => composerRef.current?.focus()) }
  const safeFiles = files.filter(isContextPath)
  const navigationCommands: PaletteCommand[] = [
    { id: 'new', title: 'New task', description: 'Start a fresh conversation', group: 'Workspace', run: newTask },
    { id: 'tasks', title: 'Task inbox', description: 'Search, pin and revisit your tasks', group: 'Workspace', run: () => navigate('tasks') },
    { id: 'changes', title: 'Review workspace changes', group: 'Workspace', run: () => navigate('workspace') },
    { id: 'plan', title: 'Plan a feature', description: 'Prepare a planning task before implementation', group: 'Actions', run: () => prepare('Plan this change: ', 'coding', 'plan') },
    { id: 'model', title: 'Model & connection settings', group: 'Settings', run: () => setSettings(true) },
    { id: 'inspector', title: 'Toggle task inspector', group: 'Workspace', run: () => setInspectorOpen(value => !value) },
    ...workbenchViews.map(item => ({ id: `view:${item.id}`, title: item.label, group: 'Workbench', run: () => navigate(item.id) })),
    ...sessions.map(session => ({ id: `task:${session.session_id}`, title: session.request || session.phase, description: session.status?.replaceAll('_',' '), group: 'Tasks', run: () => chooseSession(session.session_id) })),
  ]
  const fileCommands: PaletteCommand[] = safeFiles.map(path => ({ id: `file:${path}`, title: path, group: palette === 'context' ? 'Attach local source' : 'Files', description: palette === 'context' ? 'Add to your next task' : 'Open file in workspace', disabled: palette === 'context' && (mode === 'red_team' || contextFiles.includes(path) || contextFiles.length >= 12), run: () => { if (palette === 'context') { setContextFiles(previous => [...new Set([...previous,path])].slice(0,12)); requestAnimationFrame(() => composerRef.current?.focus()) } else void openFile(path) } }))
  const paletteCommands = palette === 'context' || palette === 'files' ? fileCommands : [...navigationCommands, ...fileCommands]
  const orderedSessions = [...sessions.filter(item => pinned.includes(item.session_id)), ...sessions.filter(item => !pinned.includes(item.session_id))]
  const viewTitle = view === 'conversation' ? current?.request || 'New task' : view === 'tasks' ? 'Tasks' : view === 'workspace' ? 'Changes' : view === 'history' ? 'History' : workbenchViews.find(item => item.id === view)?.label || 'Workspace'
  return <div className={`manager studio ${sidebarCollapsed ? 'sidebar-collapsed' : ''}`}>
    <aside className="manager-sidebar">
      <div className="sidebar-top"><button className="brand" aria-label={`${PRODUCT.name} home`} onClick={newTask}><img className="brand-mark" src="/icon.svg" alt=""/></button><span className="sidebar-caption">{PRODUCT.name}</span><button className="icon-button" aria-label="Search workspace" title="Search workspace (Ctrl / ⌘ K)" onClick={() => setPalette('commands')}><Search size={16}/></button></div>
      <button className="sidebar-new" aria-label="New task" onClick={newTask}><Plus size={16}/><span>New task</span></button>
      <nav className="main-nav" aria-label="Main navigation">
        <button aria-label="Tasks" title="Tasks" className={view === 'tasks' || view === 'history' ? 'selected' : ''} onClick={() => navigate('tasks')}><ListTodo size={16}/><span>Tasks</span><span className="count">{sessions.length}</span></button>
        <button aria-label="Changes" title="Changes" className={view === 'workspace' ? 'selected' : ''} onClick={() => navigate('workspace')}><GitBranch size={16}/><span>Changes</span><span className="count">{workspace?.files.length || 0}</span></button>
        <button className="workbench-toggle" aria-label="Workbench tools" title="Workbench tools" aria-expanded={toolsOpen} onClick={() => { if (sidebarCollapsed || window.matchMedia('(max-width: 700px)').matches) setPalette('commands'); else setToolsOpen(!toolsOpen) }}><SlidersHorizontal size={16}/><span>Workbench</span><ChevronRight size={13} className={toolsOpen ? 'rotated' : ''}/></button>
        {toolsOpen && <div className="workbench-nav">{workbenchViews.map(item => <button key={item.id} className={view === item.id ? 'selected' : ''} onClick={() => navigate(item.id)}><item.icon size={14}/><span>{item.label}</span></button>)}</div>}
      </nav>
      <div className="sidebar-section-heading"><span>Workspace</span><button className="icon-button" aria-label="Open project folder" title="Open project folder" disabled={!window.localCoder || busy} onClick={() => { void window.localCoder?.chooseProject().catch(error => setError(String(error))) }}><Plus size={14}/></button></div>
      <button className="project-row" title={project} onClick={() => navigate('workspace')}><FolderOpen size={15}/><span>{projectName}</span></button>
      <div className="sidebar-section-heading conversation-heading"><span>Recent tasks</span><button className="icon-button" aria-label="View all tasks" onClick={() => navigate('tasks')}><History size={14}/></button></div>
      <div className="sidebar-conversations">{sessionsTruncated && <p>Showing the latest 200 tasks.</p>}{orderedSessions.slice(0,30).map(session => <button key={session.session_id} className={selected === session.session_id && view === 'conversation' ? 'selected' : ''} onClick={() => chooseSession(session.session_id)} title={session.request}><span className={`status-dot ${session.status || ''}`}/><span>{session.request || session.phase}</span>{pinned.includes(session.session_id)&&<Pin size={10}/>}</button>)}{!sessions.length && <p>Your tasks will appear here.</p>}</div>
      <div className="sidebar-bottom"><button className="ide-launch" aria-label="Open IDE" disabled={!window.localCoder} title={window.localCoder ? 'Open your IDE workspace' : 'Available in the desktop app'} onClick={() => { void openIDE() }}><Code2 size={16}/><span>Open IDE</span><ArrowRight size={13}/></button><button aria-label="Settings" title="Settings" onClick={() => setSettings(true)}><Settings2 size={16}/><span>Settings</span></button></div>
    </aside>
    <main className={`manager-main ${view === 'conversation' && !current ? 'empty-conversation' : ''}`}>
      <header className="main-topbar"><div className="workspace-breadcrumb"><button className="icon-button" aria-label="Toggle sidebar" title="Toggle sidebar (Ctrl / ⌘ B)" onClick={() => setSidebarCollapsed(!sidebarCollapsed)}><PanelLeft size={17}/></button><span title={project}>{projectName}</span><ChevronRight size={12}/><strong>{viewTitle}</strong></div><div><button className="topbar-search" aria-label="Open command palette" onClick={() => setPalette('commands')}><Search size={14}/><span>Search</span><kbd>⌘ K</kbd></button><span className={`connection ${connection.toLowerCase()}`}><span className="status-dot"/>{connection === 'Connected' ? 'Engine ready' : connection}</span>{active && <button className="active-task" onClick={() => chooseSession(active.session_id)}>{stateLabel(active)}</button>}<button className="icon-button" aria-label="Toggle task inspector" onClick={() => setInspectorOpen(!inspectorOpen)}><PanelRight size={17}/></button></div></header>
      <div className="workspace-body"><div className="workspace-primary">
      {view === "tasks" ? <TaskInbox sessions={sessions} selected={selected} pinned={pinned} onSelect={chooseSession} onTogglePin={togglePin} onNew={newTask}/> : view === "doctor" ? <DoctorPanel disabled={busy} onSettings={() => setSettings(true)} /> : view === "findings" ? <FindingsWorkbench /> : view === "evaluations" ? <EvaluationPanel /> : view === "research" ? <ResearchWorkbench /> : view === "goals" ? <GoalPanel selected={goalId} onSelect={id => { setGoalId(id); setView("conversation") }} /> : view === 'memory' ? <MemoryInspector /> : view === 'harness' ? <HarnessPanel disabled={busy} mode={mode} /> : view === 'connections' ? <ConnectionsPanel /> : view === 'playbooks' ? <SecurityPlaybooks /> : view === 'history' ? <section className="history-view"><h1>Conversation history</h1><label className="history-search"><Search size={16} /><input aria-label="Search conversations" value={filter} onChange={event => setFilter(event.target.value)} placeholder="Search conversations" /></label>{sessions.filter(session => session.request?.toLowerCase().includes(filter.toLowerCase())).map(session => <button key={session.session_id} onClick={() => chooseSession(session.session_id)}><span>{session.request}</span><small>{stateLabel(session)}</small><ArrowRight size={14} /></button>)}</section> : view === 'workspace' ? <section className="workspace-review"><div className="review-files"><h2>Files & changes</h2><button type="button" onClick={() => setWorkspaceRefresh(value => value + 1)}>Refresh workspace</button><label className="file-search"><Search size={13} /><input aria-label="Filter files" value={filter} placeholder="Find a file" onChange={event => setFilter(event.target.value)} /></label><button className="view-diff" onClick={() => setSource(null)}><GitBranch size={14} />View changes <span className="count">{workspace?.files.length || 0}</span></button>{files.filter(file => file.toLowerCase().includes(filter.toLowerCase())).map(file => <button key={file} title={file} onClick={() => { void openFile(file) }}><FileCode2 size={13} /><span>{file}</span></button>)}</div><div className="review-source"><header><span>{source?.path || 'Workspace diff'}</span><span className="branch-label"><GitBranch size={12} />{workspace?.branch}</span></header>{source ? <div className="source-view">{source.content.split('\n').map((line, index) => <div className="source-line" key={index}><span>{index + 1}</span><code>{line || ' '}</code></div>)}</div> : <><div className="changed-files">{workspace?.files.map(file => <button key={file.path} onClick={() => { void openFile(file.path) }}>{file.path}<span className="additions">+{file.additions}</span><span className="deletions">−{file.deletions}</span></button>)}</div><pre className="diff-view">{diff ? diff.split('\n').map((line, index) => <div key={index} className={line.startsWith('+') ? 'diff-add' : line.startsWith('-') ? 'diff-remove' : ''}>{line || ' '}</div>) : 'No tracked changes. Select a new file above to inspect it.'}</pre></>}</div></section> : <>
      <div className="conversation-content">{quotaPaused && <aside className="quota-resume-steps"><strong>{current?.recovery?.kind === 'reconciliation_required' ? 'Recovery needs reconciliation' : 'Saved step available for recovery validation'}</strong><p>{current?.recovery?.kind === 'reconciliation_required' ? 'Inspect and reconcile pending tool effects before resuming. Changing the model does not resolve unknown workspace effects.' : 'Choose a model explicitly, set a positive task dollar cap for metered cloud use, then select Resume saved step.'}</p><div><button type="button" onClick={() => { setPreferLocal(false); setSettings(true) }}>{quotaSelectionReady ? 'Model selected · change' : 'Choose continuation model'}</button><button type="button" onClick={() => setView('harness')}>{taskDollarCap !== null && taskDollarCap > 0 ? `Task dollar cap: $${taskDollarCap}` : 'Set task dollar cap'}</button></div>{!quotaBudgetReady && <p className="hint">A positive dollar cap is required for a paid or unpriced cloud continuation. On-device models do not require a paid API budget.</p>}</aside>}{current && <><div className="user-message"><small>You</small><p>{current.request}</p></div><article className="agent-response"><header><img className="brand-mark" src="/icon.svg" alt=""/><strong>{PRODUCT.name}</strong><small className={`task-state ${current.status}`}>{stateLabel(current)}</small></header>{current.state_explanation && <p className="hint">{current.state_explanation}</p>}{current.recovery && <p className="hint">{current.recovery.explanation}</p>}{current.phase === 'plan' && current.result && current.mode !== 'red_team' && <button className="plan-handoff" disabled={busy} onClick={() => { setRequest(planDraft(current.request, current.result || '')); setMode('coding'); setAction('run'); requestAnimationFrame(() => composerRef.current?.focus()) }}><ArrowRight size={14}/>Use this plan</button>}{current.checkpoint && ['cancelled', 'interrupted', 'quota_exhausted'].includes(current.status || '') && <button className="resume-button" disabled={busy || current.recovery?.kind === 'reconciliation_required' || quota?.blocked || (quotaPaused && (!quotaSelectionReady || !quotaBudgetReady))} title={quota?.blocked ? 'Choose an available model in settings before resuming' : undefined} onClick={() => { void resume() }}>Resume saved step</button>}{tasks.length > 0 && <div className="task-checklist">{tasks.map(task => <div key={task.task_id}>{task.status === 'completed' ? <Check size={14} /> : <span className={`status-dot ${task.status || ''}`} />}<span>{task.objective}</span></div>)}</div>}{current.result ? <ReportView text={current.result}/> : current.error ? <p role="alert" className="error-text">{current.error}</p> : <p className="hint">{running(current) ? 'Working in your workspace. Activity appears below.' : 'No report was saved for this task.'}</p>}{current.engagement_id && <EngagementEvidence id={current.engagement_id} status={current.status} />}{findings.length > 0 && <details className="candidates"><summary>{findings.length} static candidates · unverified</summary>{findings.map((finding, index) => <div key={index}><span className="severity">{finding.severity}</span><p>{finding.message || finding.description}</p><button onClick={() => { if (finding.file) void openFile(finding.file) }}>{finding.file}:{finding.line}</button></div>)}</details>}<details className="activity-log" open={running(current)}><summary>Activity · {events.length} events</summary>{events.map(event => <div key={event.seq}><small>{event.source}</small><p>{event.message}</p></div>)}</details></article></>}</div>
      <div className="composer-region">{!current && <div className="home-intro"><img className="home-mark" src="/icon.svg" alt=""/><h1>What will you build?</h1><p>Plan a change. Solve a hard problem. Make something yours.</p></div>}{goalId && <div className="goal-attached">Persistent goal attached <button onClick={() => setView("goals")}>View</button><button onClick={() => setGoalId("")}>Detach</button></div>}{mode === "bug_hunting" && <BugModels disabled={busy} />}{(contextPolicy || providerUsage) && <div className="provider-context-status"><span title={`Prompt budgets use conservative UTF-8 byte bounds rather than exact tokenizer counts. The model window is its detected token limit. Source: ${contextPolicy?.source || 'Not reported'}`}>{contextPolicy ? `${modeNames[mode]} · ${contextPolicy.model_limit?.toLocaleString() ?? '?'} token window · ${contextPolicy.prompt_tokens?.toLocaleString() ?? '?'} prompt allowance / ${contextPolicy.completion_tokens?.toLocaleString() ?? '?'} output tokens` : 'Context allocation unavailable'}</span>{providerUsage && <button type="button" className={providerUsage.free_model_daily_requests && providerUsage.free_model_daily_requests.remaining <= 5 ? 'quota-low' : ''} onClick={() => setSettings(true)}>{providerUsage.free_model_daily_requests ? `Free requests: ${providerUsage.free_model_daily_requests.remaining} / ${providerUsage.free_model_daily_requests.limit} remaining today` : 'Free request allowance unknown'}</button>}</div>}{commandNotice && <aside className="composer-command-notice" role="status"><span>{commandNotice}</span><button type="button" aria-label="Dismiss command result" onClick={() => setCommandNotice('')}><X size={13}/></button></aside>}<div className="project-selector"><button title={project} disabled={!window.localCoder || busy} onClick={() => { void window.localCoder?.chooseProject().catch(error => setError(String(error))) }}>{projectName}<ChevronDown size={12} /></button></div><form className="composer" onSubmit={start}>{slashOptions.length > 0 && <div id="composer-slash-menu" className="composer-slash-menu" role="listbox" aria-label="Slash commands">{slashOptions.map((command, index) => <button type="button" role="option" aria-selected={index === slashIndex} id={`composer-slash-${index}`} key={command.name} className={index === slashIndex ? 'active' : ''} onMouseEnter={() => setSlashSelected(index)} onClick={() => { void runSlash(command.name) }}><strong>/{command.name}</strong><span>{command.description}</span></button>)}<p>↑ ↓ to navigate · Enter to choose · Esc to dismiss</p></div>}<textarea ref={composerRef} id="task-input" aria-label="Task request" placeholder={action === 'plan' ? "Describe the change. Start with a plan." : "Ask anything, or type / for commands…"} aria-controls={slashOptions.length ? "composer-slash-menu" : undefined} aria-activedescendant={slashOptions.length ? `composer-slash-${slashIndex}` : undefined} value={request} onChange={event => { setRequest(event.target.value); setSlashHidden(false); setSlashSelected(0) }} onKeyDown={event => { if (event.nativeEvent.isComposing) return; if (slashOptions.length) { if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); setSlashSelected((slashIndex + (event.key === 'ArrowDown' ? 1 : -1) + slashOptions.length) % slashOptions.length); return } if ((event.key === 'Enter' || event.key === 'Tab') && !event.ctrlKey && !event.metaKey && !event.shiftKey) { event.preventDefault(); void runSlash(slashOptions[slashIndex].name); return } if (event.key === 'Escape') { event.preventDefault(); setSlashHidden(true); return } } if (event.key === '@' && mode !== 'red_team') { event.preventDefault(); setPalette('context') } if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') event.currentTarget.form?.requestSubmit() }} rows={3} />{contextFiles.length > 0 && mode !== 'red_team' && <div className="context-chips">{contextFiles.map(path => <span key={path}><FileCode2 size={12}/>{path}<button type="button" aria-label={`Remove ${path} context`} onClick={() => setContextFiles(previous => previous.filter(file => file !== path))}><X size={11}/></button></span>)}</div>}{mode === 'red_team' && <RedTeamScope draft={engagementDraft} setDraft={setEngagementDraft} selected={engagement} select={setEngagement} disabled={busy || quota?.blocked || connection !== 'Connected'} onReviewProject={() => { void reviewProject() }} />}{mode !== 'coding' && mode !== 'red_team' && <div className="investigation-options"><label>Scope<input aria-label="Investigation target" value={target} onChange={event => setTarget(event.target.value)} /></label><label><input type="checkbox" checked={fix} onChange={event => setFix(event.target.checked)} />Apply fixes & run tests</label></div>}<div className="composer-bottom"><button type="button" className="icon-button slash-trigger" aria-label="Show slash commands" title="Commands (/)" onClick={() => { if (!request.trim()) setRequest('/'); else if (!request.startsWith('/')) setCommandNotice(slashCommands.map(item => `/${item.name} — ${item.description}`).join('\n')); setSlashHidden(false); setSlashSelected(0); composerRef.current?.focus() }}>/</button><button type="button" className="icon-button" aria-label="Attach file context" title={mode === 'red_team' ? 'Use engagement scope for source context' : 'Attach local source (@)'} disabled={mode === 'red_team'} onClick={() => setPalette('context')}><Paperclip size={16}/></button><button type="button" className="model-button" onClick={() => setSettings(true)}>{provider?.model_id || 'Connect a model'}<ChevronDown size={11} /></button><div className="composer-spacer" /><label className="mode-select">{mode === 'cybersecurity' ? <ShieldCheck size={13} /> : mode === 'bug_hunting' ? <Bug size={13} /> : <Code2 size={13} />}<select aria-label="Operating mode" value={mode} onChange={event => { setMode(event.target.value as Mode); if (event.target.value === 'red_team') { setAction('run'); setContextFiles([]) } }}>{Object.entries(modeNames).map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></label><select className="action-select" disabled={mode === 'red_team'} aria-label="Task action" value={action} onChange={event => setAction(event.target.value)}><option value="run">Run</option><option value="plan">Plan</option><option value="review">Review</option></select>{active ? <button className="send-button stop-button" type="button" aria-label="Stop task" onClick={() => { void stop() }}><Square size={13} /></button> : <button className="send-button" type="submit" aria-label="Start task" disabled={!request.trim() || (!parseSlashCommand(request) && request.trim() !== '/' && (busy || quota?.blocked || connection !== 'Connected'))}><ArrowRight size={16} /></button>}</div></form>{!current && <div className="starter-actions"><button onClick={() => prepare('Plan this feature: ', 'coding', 'plan')}><Sparkles size={14}/>Plan a feature</button><button onClick={() => prepare('Review the current workspace changes. Explain potential defects and missing tests.', 'coding', 'review')}><GitBranch size={14}/>Review changes</button><button onClick={() => prepare('Find a reproducible bug in this local project. Trace the cause and propose a focused regression test.', 'bug_hunting')}><Bug size={14}/>Find a bug</button></div>}{mode !== 'coding' && <p className="composer-caption">{mode === 'red_team' ? 'Scoped engagement · evidence journal · bounded observations' : fix ? 'Local reproduction and remediation enabled' : 'Read-only review · candidates require verification'}</p>}</div></>}
      </div>{inspectorOpen && view === 'conversation' && current && <RunInspector session={current} tasks={tasks} events={events} workspace={workspace} context={contextPolicy} onNavigate={navigate} onClose={() => setInspectorOpen(false)}/>}</div>
      {quota?.blocked && <aside className="provider-quota-notice" role="alert"><div><strong>Model quota reached</strong><p>{quota.reason || 'The selected provider cannot accept another request.'}</p><p className="hint">The daily free allowance is shared across OpenRouter free models. Wait for reset, use an on-device model, or explicitly select a metered model.</p>{quota.reset_at && <p className="hint">Reset: {new Date(typeof quota.reset_at === 'number' ? quota.reset_at * 1000 : quota.reset_at).toLocaleString()}</p>}{quota.model_id && <code>{quota.model_id}</code>}</div><div><button type="button" onClick={() => { setPreferLocal(true); setSettings(true) }}>Use an on-device model</button><button type="button" onClick={() => { setPreferLocal(false); setSettings(true) }}>Choose a model in settings</button></div></aside>}
      <ApprovalInbox connected={connection === 'Connected'} />
      {error && <div className="error-banner" role="alert"><span>{error}</span><button aria-label="Dismiss error" className="icon-button" onClick={() => setError('')}><X size={14} /></button></div>}{!window.localCoder && connection !== 'Connected' && <label className="token-entry">Server access token<input aria-label="Server access token" type="password" autoComplete="off" value={token} placeholder="For a token-protected engine" onChange={event => { setToken(event.target.value); setTokenInput(event.target.value) }} /></label>}
    </main>{palette && <CommandPalette commands={paletteCommands} onClose={() => setPalette(null)} title={palette === 'context' ? 'Add file context' : palette === 'files' ? 'Find a file' : 'Search your workspace'} placeholder={palette === 'context' || palette === 'files' ? 'Search local source files…' : 'Search actions, tasks and files…'}/>} {settings && <ProviderSettings provider={provider} mode={mode} usage={providerUsage} contextPolicy={contextPolicy} preferLocal={preferLocal} close={() => { setSettings(false); setPreferLocal(false) }} saved={value => { setProvider(value); setExplicitProvider(providerIdentity(value)); setError('') }} />}
  </div>
}
