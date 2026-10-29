import type { AgentEvent } from './api'

export type ActivityTone = 'neutral' | 'working' | 'success' | 'warning' | 'error'
export type ActivityItem = { key: number; label: string; tone: ActivityTone; events: AgentEvent[]; tool?: string }

const toolLabels: Record<string, [string, string]> = {
  read_file: ['Reading a file', 'Read a file'], search_code: ['Searching code', 'Searched code'],
  list_files: ['Finding files', 'Listed files'], edit_file: ['Editing a file', 'Edited a file'],
  write_file: ['Writing a file', 'Wrote a file'], apply_patch: ['Applying a patch', 'Applied a patch'],
  run_command: ['Running a command', 'Ran a command'], run_tests: ['Running tests', 'Ran tests'],
  lint: ['Checking lint', 'Checked lint'], build: ['Building the project', 'Built the project'],
  memory_search: ['Retrieving project memory', 'Retrieved project memory'],
  team_message: ['Coordinating task work', 'Updated task messages'],
  verify_ui: ['Checking the local interface', 'Finished the interface tool'],
}
const eventLabels: Record<string, string> = {
  iteration_started: 'Working on the next step', checkpoint_saved: 'Saved a recovery checkpoint',
  profile_selected: 'Prepared the model for this task', memory_retrieved: 'Loaded relevant project memory',
  context_pruned: 'Released older tool output', context_summarized: 'Summarized earlier context',
  context_compacted: 'Archived older context', context_snapshot: 'Saved context diagnostics',
}

function toolLabel(tool: string, complete: boolean): string {
  return toolLabels[tool]?.[complete ? 1 : 0] || `${complete ? 'Finished' : 'Using'} ${tool.replaceAll('_', ' ')}`
}

function presentation(event: AgentEvent): Omit<ActivityItem, 'events' | 'key'> {
  const called = event.event_type === 'tool_call' ? /^Calling ([a-zA-Z0-9_.-]+)$/.exec(event.message) : null
  if (called) return { label: toolLabel(called[1], false), tone: 'working', tool: called[1] }
  const result = event.event_type === 'tool_result' ? /^([a-zA-Z0-9_.-]+): (succeeded|failed)$/.exec(event.message) : null
  if (result) {
    const tool = result[1], failed = result[2] === 'failed'
    const failure = tool === 'run_tests' ? 'Tests failed' : `${tool.replaceAll('_', ' ').replace(/^[a-z]/, character => character.toUpperCase())} failed`
    return { label: failed ? failure : toolLabel(tool, true), tone: failed ? 'error' : tool === 'verify_ui' ? 'neutral' : 'success', tool }
  }
  if (/error|failed|abort/.test(event.event_type)) return { label: event.message, tone: 'error' }
  if (/warning|exhausted|blocked|refusal/.test(event.event_type)) return { label: event.message, tone: 'warning' }
  return { label: eventLabels[event.event_type] || event.message, tone: 'neutral' }
}

/** Pair only adjacent calls/results from the same component; retain every original event. */
export function activityItems(events: AgentEvent[]): ActivityItem[] {
  const items: ActivityItem[] = []
  for (const event of events) {
    const presented = presentation(event)
    // Keep long diagnostics in the original event; avoid overwhelming the conversation.
    const preview = presented.label.slice(0, 240)
    const label = presented.label.length > 240 ? preview.replace(/[\uD800-\uDBFF]$/, '') + '…' : preview
    const item = { ...presented, label, key: event.seq, events: [event] }
    const previous = items.at(-1)
    if (event.event_type === 'tool_result' && item.tool && previous?.tool === item.tool
      && previous.events.length === 1 && previous.events[0].event_type === 'tool_call'
      && previous.events[0].source === event.source) {
      items[items.length - 1] = { ...item, key: previous.key, events: [...previous.events, event] }
    } else items.push(item)
  }
  return items
}
