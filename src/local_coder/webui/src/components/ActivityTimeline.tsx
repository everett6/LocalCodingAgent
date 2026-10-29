import { useMemo, useState } from 'react'
import { Check, Circle, CircleAlert, Wrench } from 'lucide-react'
import type { AgentEvent } from '../api'
import { activityItems } from '../activityPresentation'
import type { ActivityItem } from '../activityPresentation'
import './ActivityTimeline.css'

function ActivityStep({ item }: { item: ActivityItem }) {
  const [open, setOpen] = useState(false)
  return <li className={`activity-step ${item.tone}`}>
    <span className="activity-step-icon" aria-hidden="true">{item.tone === 'error' || item.tone === 'warning' ? <CircleAlert size={14}/> : item.tone === 'success' ? <Check size={14}/> : item.tool ? <Wrench size={13}/> : <Circle size={12}/>}</span>
    <details onToggle={event => setOpen(event.currentTarget.open)}><summary>{item.label}<span className="activity-detail-hint" aria-hidden="true">Details</span></summary>{open && <div className="activity-event-detail">{item.events.map(event => <div key={event.seq}><span>{event.source}<span>{event.event_type.replaceAll('_', ' ')}</span></span><p>{event.message}</p></div>)}</div>}</details>
  </li>
}

export function ActivityTimeline({ events, active = false }: { events: AgentEvent[]; active?: boolean }) {
  const [expanded, setExpanded] = useState(false)
  const items = useMemo(() => activityItems(events), [events])
  const visible = expanded ? items : items.slice(-12)
  const hidden = items.length - visible.length
  return <details className="activity-log activity-timeline" open={active}>
    <summary>Activity<span>{events.length.toLocaleString()} events</span></summary>
    {events.length === 0 ? <p className="activity-empty">Tool activity and progress will appear here.</p> : <>
      {hidden > 0 && <button type="button" className="activity-expand" onClick={() => setExpanded(true)}>Show {hidden.toLocaleString()} earlier {hidden === 1 ? 'step' : 'steps'}</button>}
      <ol>{visible.map(item => <ActivityStep key={item.key} item={item}/>)}</ol>
      {expanded && items.length > 12 && <button type="button" className="activity-expand" onClick={() => setExpanded(false)}>Show recent steps</button>}
    </>}
  </details>
}
