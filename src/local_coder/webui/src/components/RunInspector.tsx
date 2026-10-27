import { useId, useState } from 'react'
import type { KeyboardEvent } from 'react'
import { INSPECTOR_TABS, inspectorTabForKey } from './runInspectorTabs'
import type { InspectorTab } from './runInspectorTabs'
import { Activity, ArrowUpRight, Check, FileText, GitBranch, ListChecks, PanelRightClose } from 'lucide-react'
import type { AgentEvent, ContextPolicy, Session, Workspace } from '../api'

type Task = { task_id: string; objective: string; status?: string }
type Props = { session: Session; tasks: Task[]; events: AgentEvent[]; workspace: Workspace | null; context: ContextPolicy | null; onNavigate: (view: string) => void; onClose: () => void }

export function RunInspector({session, tasks, events, workspace, context, onNavigate, onClose}: Props) {
  const [tab, setTab] = useState<InspectorTab>('overview')
  const id = useId()
  const tabId = (name: InspectorTab) => `${id}-inspector-${name}`
  const panelId = `${id}-inspector-content`
  function navigateTabs(event: KeyboardEvent<HTMLButtonElement>) {
    if (event.nativeEvent.isComposing || event.altKey || event.ctrlKey || event.metaKey) return
    const next = inspectorTabForKey(tab, event.key)
    if (!next) return
    event.preventDefault()
    setTab(next)
    event.currentTarget.ownerDocument.getElementById(tabId(next))?.focus()
  }
  const completed = tasks.filter(task => task.status === 'completed').length
  return <aside className="run-inspector" aria-label="Task inspector">
    <header><strong>Task details</strong><button type="button" className="icon-button" aria-label="Close task inspector" onClick={onClose}><PanelRightClose size={16}/></button></header>
    <div className="inspector-tabs" role="tablist" aria-label="Task detail sections" aria-orientation="horizontal">{INSPECTOR_TABS.map(name => <button type="button" key={name} id={tabId(name)} role="tab" aria-selected={tab===name} aria-controls={panelId} tabIndex={tab===name ? 0 : -1} onKeyDown={navigateTabs} onClick={()=>setTab(name)}>{name==='overview'?'Overview':name==='activity'?'Activity':'Artifacts'}</button>)}</div>
    <div className="inspector-content" id={panelId} role="tabpanel" tabIndex={0} aria-labelledby={tabId(tab)}>
      {tab==='overview' ? <><section><div className="inspector-label"><ListChecks size={14}/>Plan {tasks.length>0&&<span>{completed}/{tasks.length}</span>}</div>{tasks.length ? <ol className="inspector-plan">{tasks.map(task=><li key={task.task_id}><span className={`plan-marker ${task.status || ''}`}>{task.status==='completed'?<Check size={11}/>:null}</span><div>{task.objective}<small>{task.status?.replaceAll('_',' ') || 'Pending'}</small></div></li>)}</ol> : <p className="inspector-empty">Plan steps will appear here when the agent records them.</p>}</section>
      <section><div className="inspector-label"><GitBranch size={14}/>Workspace changes<span>{workspace ? workspace.files.length : 'Unloaded'}</span></div><p className="inspector-empty">Open the workspace to load current changes. Loaded changes may include your other work.</p><div className="inspector-files">{workspace?.files.slice(0,6).map(file=><button type="button" key={file.path} onClick={()=>onNavigate('workspace')}><span>{file.path}</span><small className="additions">+{file.additions}</small><small className="deletions">−{file.deletions}</small></button>)}</div><button type="button" className="inspector-link" onClick={()=>onNavigate('workspace')}>Review changes<ArrowUpRight size={13}/></button></section>
      <section><div className="inspector-label">Context</div><dl className="inspector-stats"><div><dt>Model window</dt><dd>{context?.model_limit?.toLocaleString() || 'Unknown'}</dd></div><div><dt>Prompt allowance</dt><dd>{context?.prompt_tokens?.toLocaleString() || 'Unknown'}</dd></div><div><dt>Output allowance</dt><dd>{context?.completion_tokens?.toLocaleString() || 'Unknown'}</dd></div></dl><button type="button" className="inspector-link" onClick={()=>onNavigate('memory')}>Inspect project memory<ArrowUpRight size={13}/></button></section></> : tab==='activity' ? <section><div className="inspector-label"><Activity size={14}/>Observed activity<span>{events.length}</span></div>{events.length ? <ol className="inspector-activity">{events.slice(-60).map(event=><li key={event.seq}><small>{event.source}</small><p>{event.message}</p></li>)}</ol> : <p className="inspector-empty">Activity appears as tools and agents report progress.</p>}</section> : <section><div className="inspector-label"><FileText size={14}/>Task artifacts</div><div className="artifact-row"><FileText size={20}/><div><strong>{session.phase==='plan'?'Implementation plan':'Task report'}</strong><small>{session.result?'Available in the conversation':'Waiting for a saved result'}</small></div></div>{session.checkpoint&&<div className="artifact-row"><ListChecks size={20}/><div><strong>Saved agent step</strong><small>Recovery state retained for this task</small></div></div>}<p className="inspector-empty">Copy and download the saved report from the conversation. Checkpoints restore agent state; they do not undo workspace files.</p></section>}
    </div>
  </aside>
}
