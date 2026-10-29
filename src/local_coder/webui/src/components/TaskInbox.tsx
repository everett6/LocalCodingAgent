import { useMemo, useState } from 'react'
import { CircleAlert, CircleCheck, CircleDot, Clock3, FileText, Pin, PinOff, Plus, Search } from 'lucide-react'
import type { Session } from '../api'
import { inboxCounts, selectInboxTasks, taskMatchesFilter, taskModeLabel, taskStatusLabel, taskTitle } from '../taskInbox'
import type { InboxFilter } from '../taskInbox'

type Props = {
  sessions: Session[]; selected: string; pinned: string[]
  onSelect: (id: string) => void; onTogglePin: (id: string) => void; onNew: () => void
}

const filters: { id: InboxFilter; label: string }[] = [
  { id: 'all', label: 'All' }, { id: 'active', label: 'Active' },
  { id: 'attention', label: 'Needs attention' }, { id: 'completed', label: 'Completed' },
]

function StatusIcon({ session }: { session: Session }) {
  const Icon = session.status === 'queued' ? Clock3 : taskMatchesFilter(session, 'active') ? CircleDot
    : taskMatchesFilter(session, 'attention') ? CircleAlert : session.status === 'completed' ? CircleCheck : FileText
  return <Icon size={18} aria-hidden="true" />
}

export function TaskInbox({ sessions, selected, pinned, onSelect, onTogglePin, onNew }: Props) {
  const [search, setSearch] = useState(''), [filter, setFilter] = useState<InboxFilter>('all')
  const tasks = useMemo(() => selectInboxTasks(sessions, pinned, search, filter), [sessions, pinned, search, filter])
  const counts = useMemo(() => inboxCounts(sessions, search), [sessions, search])
  const pins = useMemo(() => new Set(pinned), [pinned])
  function clearFilters() { setSearch(''); setFilter('all') }
  return <section className="task-inbox" aria-labelledby="task-inbox-title">
    <header className="inbox-header">
      <div><h1 id="task-inbox-title">Tasks</h1><p>Pick up work, review results, and find tasks that need your attention.</p></div>
      <button type="button" onClick={onNew}><Plus size={16} aria-hidden="true" />New task</button>
    </header>
    <div className="inbox-toolbar">
      <label><Search size={16} aria-hidden="true" /><input type="search" aria-label="Search tasks" placeholder="Search tasks, modes, or status" value={search} onChange={event => setSearch(event.target.value)} /></label>
      <div className="inbox-filters" role="group" aria-label="Filter tasks by status">{filters.map(item => <button type="button" key={item.id} aria-pressed={filter === item.id} onClick={() => setFilter(item.id)}>{item.label}<span>{counts[item.id]}</span></button>)}</div>
    </div>
    {tasks.length ? <ul className="inbox-list" aria-label="Tasks">{tasks.map(session => <li key={session.session_id} className={`inbox-row${selected === session.session_id ? ' selected' : ''}`}>
      <button type="button" className="inbox-open" aria-current={selected === session.session_id ? 'page' : undefined} onClick={() => onSelect(session.session_id)}>
        <StatusIcon session={session} />
        <span><strong title={session.request}>{taskTitle(session)}</strong><span className="inbox-meta"><span>{taskModeLabel(session)}</span><span>{taskStatusLabel(session)}</span></span></span>
      </button>
      <button type="button" className="inbox-pin" aria-label={`${pins.has(session.session_id) ? 'Unpin' : 'Pin'} task: ${taskTitle(session)}`} aria-pressed={pins.has(session.session_id)} title={pins.has(session.session_id) ? 'Unpin task' : 'Pin task'} onClick={() => onTogglePin(session.session_id)}>{pins.has(session.session_id) ? <PinOff size={16} aria-hidden="true" /> : <Pin size={16} aria-hidden="true" />}</button>
    </li>)}</ul> : <div className="inbox-empty">
      <FileText size={28} aria-hidden="true" />
      <h2>{sessions.length ? 'No tasks match this view' : 'Start your first task'}</h2>
      <p>{sessions.length ? 'Try another search or clear the status filter to see more work.' : 'Describe what you want to build or investigate. Your tasks and their results will appear here.'}</p>
      {sessions.length ? <button type="button" onClick={clearFilters}>Clear search and filters</button> : <button type="button" onClick={onNew}>New task</button>}
    </div>}
  </section>
}
