import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ArrowRight, Bug, Check, CheckCircle2, ChevronDown, Circle, Code2, FileCode2, FolderOpen, GitBranch, History, LayoutPanelLeft, LoaderCircle, Plus, Search, Settings2, ShieldCheck, Square, Undo2, X } from 'lucide-react'
import { api, running, setToken } from './api'
import type { AgentEvent, Mode, Provider, Session, Workspace } from './api'
import { ProviderSettings } from './components/ProviderSettings'
import { RedTeamScope, EngagementEvidence } from './components/RedTeamScope'
import { emptyEngagement, engagementPayload } from './redteam'
import { SecurityPlaybooks } from './components/SecurityPlaybooks'
import './App.css'
import './AppEnhancements.css'
type Task = { task_id: string; objective: string; status?: string }
type Finding = { file?: string; line?: number; severity?: string; rule_id?: string; message?: string; description?: string }
const modeNames: Record<Mode, string> = { coding: 'Build', cybersecurity: 'Cybersecurity', bug_hunting: 'Bug hunting', red_team: 'Red Team' }
function connectionErrorMessage(error: unknown) {
  const message = error instanceof Error ? error.message : 'Cannot connect to the local engine'
  return message.startsWith('Unexpected token') && message.includes('<!doctype')
    ? 'This address serves the web UI, not the Local Coder engine API. Start the engine and open its address.'
    : message
}
export default function App() {
  const [sessions, setSessions] = useState<Session[]>([]), [selected, setSelected] = useState(''), [events, setEvents] = useState<AgentEvent[]>([])
  const [provider, setProvider] = useState<Provider | null>(null), [workspace, setWorkspace] = useState<Workspace | null>(null)
  const [project, setProject] = useState(''), [files, setFiles] = useState<string[]>([]), [filter, setFilter] = useState('')
  const [source, setSource] = useState<{ path: string; content: string } | null>(null), [diff, setDiff] = useState('')
  const [view, setView] = useState('conversation'), [mode, setMode] = useState<Mode>('coding'), [request, setRequest] = useState(''), [target, setTarget] = useState('.'), [fix, setFix] = useState(false), [action, setAction] = useState('run')
  const [settings, setSettings] = useState(false), [projectHelp, setProjectHelp] = useState(false), [mentionQuery, setMentionQuery] = useState<string | null>(null), [selectedDiffPath, setSelectedDiffPath] = useState(''), [error, setError] = useState(''), [connection, setConnection] = useState('Connecting'), [sending, setSending] = useState(false), [token, setTokenInput] = useState(''), [refreshKey, setRefreshKey] = useState(0), [clock, setClock] = useState(0)
  const [engagement, setEngagement] = useState(''), [engagementDraft, setEngagementDraft] = useState(emptyEngagement)
  const sequence = useRef(0), instance = useRef(''), taskInput = useRef<HTMLTextAreaElement>(null)
  const current = sessions.find(session => session.session_id === selected), active = sessions.find(running), busy = !!active || sending
  const chooseSession = useCallback((id: string) => { setView('conversation'); if (id === selected) return; sequence.current = 0; setEvents([]); setSelected(id) }, [selected])
  useEffect(() => {
    let stopped = false
    let timer: ReturnType<typeof setTimeout>
    async function poll() {
      try {
        const [status, config, tree, changes, listing] = await Promise.all([api<{ project_root: string; sessions: Session[] }>('/status'), api<{ provider: Provider | null }>('/settings'), api<Workspace>('/workspace'), api<{ diff: string }>(`/diff${selectedDiffPath ? `?path=${encodeURIComponent(selectedDiffPath)}` : ''}`), api<{ files: string[] }>('/files')])
        if (stopped) return
        setSessions(status.sessions); setProject(status.project_root); setProvider(config.provider); setWorkspace(tree); setDiff(changes.diff); setFiles(listing.files)
        if (selected) {
          const result = await api<{ events: AgentEvent[]; latest_seq: number; instance_id: string }>(`/events?session_id=${encodeURIComponent(selected)}&since=${sequence.current}`)
          if (stopped) return
          if (instance.current && instance.current !== result.instance_id) { sequence.current = 0; setEvents([]) }
          else { sequence.current = result.latest_seq; setEvents(previous => [...previous, ...result.events].slice(-600)) }
          instance.current = result.instance_id
        }
        setConnection('Connected')
      } catch (error) { if (!stopped) { setConnection('Disconnected'); setError(connectionErrorMessage(error)) } }
      finally { if (!stopped) timer = setTimeout(poll, 1800) }
    }
    void poll()
    return () => { stopped = true; clearTimeout(timer) }
  }, [selected, token, refreshKey, selectedDiffPath])
  useEffect(() => {
    function shortcuts(event: KeyboardEvent) {
      if (!(event.metaKey || event.ctrlKey) || event.altKey) return
      if (event.key.toLowerCase() === 'k' && !event.shiftKey) {
        event.preventDefault()
        taskInput.current?.focus()
      } else if (event.shiftKey && event.key.toLowerCase() === 'n') {
        event.preventDefault()
        chooseSession('')
        taskInput.current?.focus()
      } else if (event.shiftKey && event.key.toLowerCase() === 'e') {
        event.preventDefault()
        setView('workspace')
      }
    }
    window.addEventListener('keydown', shortcuts)
    return () => window.removeEventListener('keydown', shortcuts)
  }, [chooseSession])
  useEffect(() => {
    const cursor = taskInput.current?.selectionStart ?? request.length
    const match = request.slice(0, cursor).match(/(?:^|\s)@([^\s@]*)$/)
    setMentionQuery(match ? match[1] : null)
  }, [request])
  const activeSessionId = active?.session_id
  useEffect(() => {
    if (!activeSessionId) return
    const timer = setInterval(() => setClock(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [activeSessionId])
  const tasks = useMemo(() => { const result = new Map<string, Task>(); events.forEach(event => { if (event.event_type === 'plan_created' && Array.isArray(event.data.tasks)) (event.data.tasks as Task[]).forEach(task => result.set(task.task_id, task)); if (event.event_type === 'task_status') { const id = String(event.data.task_id), task = result.get(id); if (task) result.set(id, { ...task, status: String(event.data.status) }) } }); return [...result.values()] }, [events])
  const findings = useMemo(() => { const result = new Map<string, Finding>(); events.forEach(event => { if (event.event_type === 'scan_completed' && Array.isArray(event.data.findings)) (event.data.findings as Finding[]).filter(f => f.file).forEach(f => result.set(`${f.file}:${f.line}:${f.rule_id || f.description}`, f)) }); return [...result.values()] }, [events])
  async function openFile(path: string) {
    if (view === 'workspace' && workspace?.files.some(file => file.path === path)) return openDiff(path)
    try { setSource(await api(`/file?path=${encodeURIComponent(path)}`)); setView('workspace') } catch (error) { setError(error instanceof Error ? error.message : 'Cannot open file') }
  }
  async function openDiff(path: string) {
    try {
      const selectedFile = await api<{ path: string; diff: string }>(`/diff?path=${encodeURIComponent(path)}`)
      setSelectedDiffPath(selectedFile.path)
      setSource(null)
      setDiff(selectedFile.diff)
    } catch (error) { setError(error instanceof Error ? error.message : 'Cannot load file changes') }
  }
  function insertMention(path: string) {
    const input = taskInput.current
    if (!input) return
    const cursor = input.selectionStart
    const before = request.slice(0, cursor)
    const mentionStart = before.lastIndexOf('@')
    const next = `${before.slice(0, mentionStart)}@${path} ${request.slice(input.selectionEnd)}`
    setRequest(next)
    setMentionQuery(null)
    window.requestAnimationFrame(() => {
      input.focus()
      const position = mentionStart + path.length + 2
      input.setSelectionRange(position, position)
    })
  }
  async function start(event: React.FormEvent) { event.preventDefault(); if (busy || !request.trim()) return; setSending(true); setError(''); try { let engagementId = engagement; if (mode === 'red_team' && !engagementId) { const created = await api<{ id: string }>('/engagements', 'POST', engagementPayload(engagementDraft, request)); engagementId = created.id; setEngagement(created.id) } const job = await api<Session>('/jobs', 'POST', { request, mode, action: mode === 'red_team' ? 'run' : action, target_path: target || '.', fix: mode === 'red_team' ? false : fix, engagement_id: mode === 'red_team' ? engagementId : undefined, parent_session_id: mode === 'red_team' ? (current?.engagement_id === engagementId ? selected : undefined) : selected || undefined }); setSessions(previous => [job, ...previous]); chooseSession(job.session_id); setRequest('') } catch (error) { setError(error instanceof Error ? error.message : 'Cannot start task') } finally { setSending(false) } }
  async function resume() { if (!current?.checkpoint || busy) return; setSending(true); try { const job = await api<Session>('/jobs', 'POST', { request: `Resume: ${current.request}`, resume_checkpoint: current.checkpoint, mode: current.mode || mode, engagement_id: current.engagement_id }); setSessions(previous => [job, ...previous]); chooseSession(job.session_id) } catch (error) { setError(error instanceof Error ? error.message : 'Cannot resume checkpoint') } finally { setSending(false) } }
  async function stop() { if (active) try { await api(`/jobs/${active.session_id}/cancel`, 'POST', {}) } catch (error) { setError(error instanceof Error ? error.message : 'Cannot stop task') } }
  async function undoLatestRun() {
    if (!current?.session_id || !window.confirm('Restore the workspace to immediately before this task? Undo is refused if the workspace changed after the task finished.')) return
    try {
      await api('/undo', 'POST', { session_id: current.session_id })
      setSessions(previous => previous.map(session => session.session_id === current.session_id ? { ...session, undo_applied: true } : session))
      setSource(null)
      setError('')
    } catch (error) { setError(error instanceof Error ? error.message : 'Cannot undo workspace changes') }
  }
  function retryCurrentTask() {
    if (!current) return
    setMode(current.mode || 'coding')
    setRequest(current.request)
    setView('conversation')
    window.setTimeout(() => taskInput.current?.focus(), 0)
  }
  async function openIDE() { try { await window.localCoder?.openIDE() } catch (error) { setError(error instanceof Error ? error.message : 'Cannot open the IDE') } }
  const projectCommand = `cd ${project ? `'${project.replaceAll("'", "'\\''")}'` : "'/path/to/project'"} && local-coder --project . serve --port 8787`
  async function copyProjectCommand() {
    try { await navigator.clipboard.writeText(projectCommand) } catch (error) { setError(error instanceof Error ? `Cannot copy command: ${error.message}` : 'Cannot copy command') }
  }
  const projectName = project.split(/[\\/]/).filter(Boolean).pop() || 'No project'
  const starterPrompts = ['Fix a failing test', 'Explain this codebase', 'Review my recent changes']
  const latestProgressEvent = [...events].reverse().find(event => ['phase', 'tool_call', 'approval_required', 'task_status'].includes(event.event_type))
  const progressLabel = latestProgressEvent?.message || (current?.status === 'queued' ? 'Waiting for an available agent' : 'Starting task')
  const elapsedSeconds = active?.started_at ? Math.max(0, Math.floor((clock / 1000) - active.started_at)) : 0
  const elapsedLabel = `${Math.floor(elapsedSeconds / 60)}:${String(elapsedSeconds % 60).padStart(2, '0')}`
  const testSummary = current?.result?.match(/Verification:\s*(passed|failed)(?:\s*\nTests:\s*([^\n]*))?/i)
  const hasUndoCheckpoint = !current?.undo_applied && (!!current?.pre_run_checkpoint_id || events.some(event => event.event_type === 'checkpoint_created'))
  const mentionMatches = mentionQuery === null ? [] : files.filter(file => !file.startsWith('.local-coder/') && !file.split('/').some(part => part.startsWith('.env') || part.endsWith('.pem') || part.endsWith('.key')) && file.toLowerCase().startsWith(mentionQuery.toLowerCase())).slice(0, 8)
  const setupSteps = [
    { label: 'Engine connected', done: connection === 'Connected', action: connection !== 'Connected' ? 'Retry connection' : '', onClick: () => { setError(''); setConnection('Connecting'); setRefreshKey(value => value + 1) } },
    { label: 'Project selected', done: !!project && projectName !== 'No project', action: projectName === 'No project' ? (window.localCoder ? 'Choose project' : 'Open engine from a project folder') : '', onClick: () => { if (window.localCoder) void window.localCoder.chooseProject().catch(error => setError(String(error))); else setProjectHelp(true) } },
    { label: 'Model configured', done: !!provider?.model_id, action: !provider?.model_id ? 'Configure model' : '', onClick: () => setSettings(true) },
  ]
  const needsSetup = setupSteps.some(step => !step.done)
  return <div className="manager">
    {projectHelp && <div className="project-help-backdrop" role="presentation" onClick={() => setProjectHelp(false)}><section className="project-help" role="dialog" aria-modal="true" aria-labelledby="project-help-title" onClick={event => event.stopPropagation()}><button className="icon-button project-help-close" aria-label="Close project setup" onClick={() => setProjectHelp(false)}><X size={15} /></button><h2 id="project-help-title">Choose a project folder</h2><p>The browser workspace follows the folder used to start the Local Coder engine. Stop the engine, open a terminal in the project you want, then restart it with:</p><code>{projectCommand}</code><div><button onClick={() => { void copyProjectCommand() }}>Copy command</button><button onClick={() => setProjectHelp(false)}>Done</button></div></section></div>}
    {mentionQuery !== null && mentionMatches.length > 0 && <div className="mention-suggestions" role="listbox" aria-label="Files to mention">{mentionMatches.map(file => <button key={file} type="button" role="option" onMouseDown={event => event.preventDefault()} onClick={() => insertMention(file)}><FileCode2 size={13} />{file}</button>)}</div>}
    <div className="shortcut-hints" aria-label="Keyboard shortcuts">Ctrl/Cmd+K task · Ctrl/Cmd+Shift+N new chat · Ctrl/Cmd+Shift+E workspace</div>
    {!window.localCoder && <button className="project-help-trigger" type="button" onClick={() => setProjectHelp(true)}>Switch project</button>}
    {view === 'workspace' && selectedDiffPath && <button className="all-changes-trigger" type="button" onClick={() => { setSelectedDiffPath(''); setSource(null) }}>View all changes</button>}
    <aside className="manager-sidebar"><div className="sidebar-top"><button className="brand" aria-label="Local Coder home" onClick={() => chooseSession('')}><span className="brand-mark">Λ</span></button><span className="sidebar-caption">Local Coder</span><button className="icon-button" aria-label="Conversation history" onClick={() => setView('history')}><History size={15} /></button></div>
      <nav className="main-nav" aria-label="Main navigation"><button aria-label="New conversation" title="New conversation" className={view === 'conversation' && !selected ? 'selected' : ''} onClick={() => chooseSession('')}><Plus size={15} />New conversation</button><button aria-label="Conversation history" title="Conversation history" className={view === 'history' ? 'selected' : ''} onClick={() => setView('history')}><History size={14} />Conversation history</button><button aria-label="Workspace changes" title="Workspace changes" className={view === 'workspace' ? 'selected' : ''} onClick={() => setView('workspace')}><GitBranch size={14} />Workspace changes<span className="count">{workspace?.files.length || 0}</span></button><button aria-label="Security playbooks" title="Security playbooks" onClick={() => setView('playbooks')}><ShieldCheck size={14} />Security playbooks</button><button aria-label="Customizations" title="Customizations" onClick={() => setSettings(true)}><Settings2 size={14} />Customizations</button></nav>
      <div className="sidebar-section-heading"><span>Projects</span><button className="icon-button" aria-label="Open project folder" title={window.localCoder ? 'Open project folder' : 'Choose a project when starting the engine'} disabled={!window.localCoder || busy} onClick={() => { void window.localCoder?.chooseProject().catch(error => setError(String(error))) }}><FolderOpen size={14} /></button></div><button className="project-row" title={project} onClick={() => setView('workspace')}><FolderOpen size={14} /><span>{projectName}</span></button>
      <div className="sidebar-section-heading conversation-heading"><span>Conversations</span><button className="icon-button" aria-label="New conversation" onClick={() => chooseSession('')}><Plus size={14} /></button></div><div className="sidebar-conversations">{sessions.map(session => <button key={session.session_id} className={selected === session.session_id ? 'selected' : ''} onClick={() => chooseSession(session.session_id)} title={session.request}><span className={`status-dot ${session.status || ''}`} /><span>{session.request || session.phase}</span></button>)}{!sessions.length && <p>No conversations yet</p>}</div>
      <div className="sidebar-bottom"><button className="ide-launch" aria-label="Open IDE" disabled={!window.localCoder} title={window.localCoder ? 'Open the Local Coder Code OSS fork' : 'Launch the Code OSS fork from the desktop app'} onClick={() => { void openIDE() }}><Code2 size={15} />Open IDE<ArrowRight size={13} /></button><button aria-label="Settings" title="Settings" onClick={() => setSettings(true)}><Settings2 size={14} />Settings</button></div>
    </aside>
    <main className={`manager-main ${view === 'conversation' && !current ? 'empty-conversation' : ''} ${view === 'conversation' && !current && mode === 'coding' ? 'has-welcome' : ''}`}><header className="main-topbar"><span>{view === 'history' ? 'Conversation history' : view === 'workspace' ? projectName : current?.request || ''}</span><div><span className={`connection ${connection.toLowerCase()}`}><span className="status-dot" />{connection}</span>{active && <button className="active-task" onClick={() => chooseSession(active.session_id)}>Task running</button>}<button className="icon-button" aria-label="Open IDE" disabled={!window.localCoder} onClick={() => { void openIDE() }}><LayoutPanelLeft size={16} /></button></div></header>
      {view === 'playbooks' ? <SecurityPlaybooks /> : view === 'history' ? <section className="history-view"><h1>Conversation history</h1><label className="history-search"><Search size={16} /><input aria-label="Search conversations" value={filter} onChange={event => setFilter(event.target.value)} placeholder="Search conversations" /></label>{sessions.filter(session => session.request?.toLowerCase().includes(filter.toLowerCase())).map(session => <button key={session.session_id} onClick={() => chooseSession(session.session_id)}><span>{session.request}</span><small>{session.status || session.phase}</small><ArrowRight size={14} /></button>)}</section> : view === 'workspace' ? <section className="workspace-review"><div className="review-files"><h2>Files & changes <span>{workspace?.files.length || 0}</span></h2><div className="change-totals"><span className="additions">+{workspace?.total_additions || 0}</span><span className="deletions">−{workspace?.total_deletions || 0}</span><small>{workspace?.files.length || 0} files changed</small></div><label className="file-search"><Search size={13} /><input aria-label="Filter files" value={filter} placeholder="Find a file" onChange={event => setFilter(event.target.value)} /></label><button className="view-diff" aria-label="View complete diff" onClick={() => setSource(null)}><GitBranch size={14} />View changes <span className="count">{workspace?.files.length || 0}</span></button>{files.filter(file => file.toLowerCase().includes(filter.toLowerCase())).map(file => <button key={file} title={file} onClick={() => { void openFile(file) }}><FileCode2 size={13} /><span>{file}</span></button>)}</div><div className="review-source"><header><span>{source?.path || 'Workspace diff'}</span><div className="review-actions">{workspace?.files.length ? <span className="change-count">{workspace.files.length} changed</span> : null}{hasUndoCheckpoint && !busy && <button className="undo-button" onClick={() => { void undoLatestRun() }}><Undo2 size={13} />Undo run</button>}<span className="branch-label"><GitBranch size={12} />{workspace?.branch}</span></div></header>{source ? <div className="source-view">{source.content.split('\n').map((line, index) => <div className="source-line" key={index}><span>{index + 1}</span><code>{line || ' '}</code></div>)}</div> : <><div className="changed-files">{workspace?.files.map(file => <button key={file.path} onClick={() => { void openFile(file.path) }}>{file.path}<span className="additions">+{file.additions}</span><span className="deletions">−{file.deletions}</span></button>)}</div><pre className="diff-view">{diff ? diff.split('\n').map((line, index) => <div key={index} className={line.startsWith('+') ? 'diff-add' : line.startsWith('-') ? 'diff-remove' : ''}>{line || ' '}</div>) : 'No tracked changes. Select a new file above to inspect it.'}</pre></>}</div></section> : <>
      <div className="conversation-content">{!current && mode === 'coding' && <section className="welcome-panel"><span className="welcome-eyebrow"><Code2 size={14} /> YOUR LOCAL CODING PARTNER</span><h1>What should we build?</h1><p>{connection === 'Connected' ? `Ask Local Coder to work in ${projectName}, run checks, and show you the changes.` : 'Connect to your Local Coder engine to start a task. You can draft a request while it reconnects.'}</p>{needsSetup && <div className="setup-checklist" aria-label="Setup checklist"><h2>Get ready to code</h2>{setupSteps.map((step, index) => <div className="setup-step" key={step.label}><span className={step.done ? 'setup-step-icon done' : 'setup-step-icon'}>{step.done ? <CheckCircle2 size={15} /> : <Circle size={15} />}</span><span className={step.done ? 'setup-step-label done' : 'setup-step-label'}>{index + 1}. {step.label}</span>{step.action && <button type="button" disabled={index === 1 && !window.localCoder} title={step.action} onClick={step.onClick}>{step.action}</button>}</div>)}</div>}<div className="starter-prompts" aria-label="Suggested tasks">{starterPrompts.map(prompt => <button key={prompt} type="button" onClick={() => { setRequest(prompt); taskInput.current?.focus() }}>{prompt}<ArrowRight size={13} /></button>)}</div></section>}{current && <><div className="user-message"><small>You</small><p>{current.request}</p></div><article className="agent-response"><header><span className="brand-mark">Λ</span><strong>Local Coder</strong><small className={`task-state ${current.status}`}>{current.status?.replaceAll('_', ' ') || 'Saved'}</small></header>{running(current) && <section className="run-progress" aria-live="polite"><div className="progress-heading"><LoaderCircle size={15} /><strong>{current.status === 'queued' ? 'Queued' : 'Working'}</strong><time>{elapsedLabel}</time></div><p>{progressLabel}</p>{tasks.length > 0 && <small>{tasks.filter(task => task.status === 'completed').length} of {tasks.length} steps complete</small>}</section>}{current.checkpoint && ['cancelled', 'interrupted'].includes(current.status || '') && <button className="resume-button" disabled={busy} onClick={() => { void resume() }}>Resume saved step</button>}{['failed', 'cancelled', 'interrupted'].includes(current.status || '') && <button className="retry-button" disabled={busy} onClick={retryCurrentTask}>Retry this request</button>}{tasks.length > 0 && <div className="task-checklist">{tasks.map(task => <div key={task.task_id}>{task.status === 'completed' ? <Check size={14} /> : <span className={`status-dot ${task.status || ''}`} />}<span>{task.objective}</span></div>)}</div>}{current.result ? <pre className="report">{current.result}</pre> : current.error ? <p role="alert" className="error-text">{current.error}</p> : <p className="hint">{running(current) ? 'Working in your workspace. Activity appears below.' : 'No report was saved for this task.'}</p>}{testSummary && <section className={`test-summary ${testSummary[1].toLowerCase()}`}><strong>{testSummary[1].toLowerCase() === 'passed' ? 'Tests passed' : 'Tests need attention'}</strong>{testSummary[2] && <span>{testSummary[2]}</span>}</section>}{current.engagement_id && <EngagementEvidence id={current.engagement_id} status={current.status} />}{findings.length > 0 && <details className="candidates"><summary>{findings.length} static candidates · unverified</summary>{findings.map((finding, index) => <div key={index}><span className="severity">{finding.severity}</span><p>{finding.message || finding.description}</p><button onClick={() => { if (finding.file) void openFile(finding.file) }}>{finding.file}:{finding.line}</button></div>)}</details>}<details className="activity-log" open={running(current)}><summary>Activity · {events.length} events</summary>{events.map(event => <div key={event.seq}><small>{event.source}</small><p>{event.message}</p></div>)}</details></article></>}</div>
      <div className="composer-region"><div className="project-selector"><button title={project} disabled={!window.localCoder || busy} onClick={() => { void window.localCoder?.chooseProject().catch(error => setError(String(error))) }}>{projectName}<ChevronDown size={12} /></button></div><form className="composer" onSubmit={start}><textarea ref={taskInput} id="task-input" aria-label="Task request" placeholder="Ask anything, @ to mention a file, / for actions" value={request} onChange={event => setRequest(event.target.value)} onKeyDown={event => { if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') event.currentTarget.form?.requestSubmit() }} rows={2} />{mode === 'red_team' && <RedTeamScope draft={engagementDraft} setDraft={setEngagementDraft} selected={engagement} select={setEngagement} disabled={busy} />}{mode !== 'coding' && mode !== 'red_team' && <div className="investigation-options"><label>Scope<input aria-label="Investigation target" value={target} onChange={event => setTarget(event.target.value)} /></label><label><input type="checkbox" checked={fix} onChange={event => setFix(event.target.checked)} />Apply fixes & run tests</label></div>}<div className="composer-bottom"><button type="button" className="icon-button" aria-label="Provider settings" onClick={() => setSettings(true)}><Plus size={15} /></button><button type="button" className="model-button" onClick={() => setSettings(true)}>{provider?.model_id || 'Connect a model'}<ChevronDown size={11} /></button><div className="composer-spacer" /><label className="mode-select">{mode === 'cybersecurity' ? <ShieldCheck size={13} /> : mode === 'bug_hunting' ? <Bug size={13} /> : <Code2 size={13} />}<select aria-label="Operating mode" value={mode} onChange={event => { setMode(event.target.value as Mode); if (event.target.value === 'red_team') setAction('run') }}>{Object.entries(modeNames).map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></label><select className="action-select" disabled={mode === 'red_team'} aria-label="Task action" value={action} onChange={event => setAction(event.target.value)}><option value="run">Run</option><option value="plan">Plan</option><option value="review">Review</option></select>{active ? <button className="send-button stop-button" type="button" aria-label="Stop task" onClick={() => { void stop() }}><Square size={13} /></button> : <button className="send-button" type="submit" aria-label="Start task" disabled={busy || !request.trim() || connection !== 'Connected'}><ArrowRight size={16} /></button>}</div></form>{mode !== 'coding' && <p className="composer-caption">{mode === 'red_team' ? 'Scoped engagement · evidence journal · bounded observations' : fix ? 'Local reproduction and remediation enabled' : 'Read-only review · candidates require verification'}</p>}</div></>}
      {error && <div className="error-banner" role="alert"><span>{error}</span><button aria-label="Dismiss error" className="icon-button" onClick={() => setError('')}><X size={14} /></button></div>}{!window.localCoder && connection !== 'Connected' && <label className="token-entry">Server access token<input aria-label="Server access token" type="password" autoComplete="off" value={token} placeholder="For a token-protected engine" onChange={event => { setToken(event.target.value); setTokenInput(event.target.value) }} /></label>}
    </main>{settings && <ProviderSettings provider={provider} close={() => setSettings(false)} saved={value => { setProvider(value); setError('') }} />}
  </div>
}
