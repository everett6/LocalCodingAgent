import type { Mode, Session } from './api'
import { stateLabel } from './workspaceUI.ts'

export type InboxFilter = 'all' | 'active' | 'attention' | 'completed'
export type InboxCounts = Record<InboxFilter, number>

const activeStatuses = new Set(['queued', 'running', 'cancelling'])
const attentionStatuses = new Set(['blocked', 'quota_exhausted', 'interrupted', 'requires_continuation', 'failed', 'cancelled'])
const statusNames: Record<string, string> = {
  queued: 'Queued', running: 'In progress', cancelling: 'Stopping', completed: 'Completed',
  blocked: 'Blocked', quota_exhausted: 'Quota reached', interrupted: 'Interrupted',
  requires_continuation: 'Needs continuation', failed: 'Failed', cancelled: 'Cancelled',
}
const modeNames: Record<Mode, string> = { coding: 'Build', cybersecurity: 'Cybersecurity', bug_hunting: 'Bug hunting', red_team: 'Red team' }

export function taskStatusLabel(session: Session): string {
  if (session.observed_state) return stateLabel(session)
  const status = session.status || ''
  return statusNames[status] || (status ? status.replaceAll('_', ' ') : 'Saved')
}

export function taskModeLabel(session: Session): string {
  return session.mode ? modeNames[session.mode] || session.mode.replaceAll('_', ' ') : 'Task'
}

export function taskTitle(session: Session): string {
  const title = session.request.replace(/\s+/gu, ' ').trim()
  if (!title) return 'Untitled task'
  const characters = Array.from(title)
  return characters.length > 112 ? characters.slice(0, 111).join('') + '…' : title
}

export function taskMatchesFilter(session: Session, filter: InboxFilter): boolean {
  if (filter === 'all') return true
  const status = session.status || ''
  if (filter === 'active') return activeStatuses.has(status)
  if (filter === 'attention') return attentionStatuses.has(status)
  return status === 'completed'
}

export function taskMatchesSearch(session: Session, search: string): boolean {
  const terms = search.trim().toLocaleLowerCase().split(/\s+/u).filter(Boolean)
  const text = [session.request, session.phase, session.mode || '', session.status || '',
    taskModeLabel(session), taskStatusLabel(session)].join(' ').replaceAll('_', ' ').toLocaleLowerCase()
  return terms.every(term => text.includes(term.replaceAll('_', ' ')))
}

export function inboxCounts(sessions: readonly Session[], search = ''): InboxCounts {
  const counts: InboxCounts = { all: 0, active: 0, attention: 0, completed: 0 }
  for (const session of sessions) {
    if (!taskMatchesSearch(session, search)) continue
    counts.all += 1
    for (const filter of ['active', 'attention', 'completed'] as const) {
      if (taskMatchesFilter(session, filter)) counts[filter] += 1
    }
  }
  return counts
}

export function selectInboxTasks(sessions: readonly Session[], pinned: readonly string[], search = '', filter: InboxFilter = 'all'): Session[] {
  const pins = new Set(pinned)
  const matches = sessions.filter(session => taskMatchesSearch(session, search) && taskMatchesFilter(session, filter))
  // Stable partitions preserve the host's order, including ties and new tasks.
  return [...matches.filter(session => pins.has(session.session_id)), ...matches.filter(session => !pins.has(session.session_id))]
}
