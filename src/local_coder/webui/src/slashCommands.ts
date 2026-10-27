export const slashCommands = [
  { name: 'status', description: 'Show engine, model and active task status' },
  { name: 'model', description: 'Choose a model or provider' },
  { name: 'local', description: 'Open your local model library' },
  { name: 'context', description: 'Inspect context, tokens and task limits' },
  { name: 'memory', description: 'Search remembered project evidence' },
  { name: 'plan', description: 'Prepare a planning prompt', acceptsText: true },
  { name: 'review', description: 'Prepare a code review prompt', acceptsText: true },
  { name: 'run', description: 'Prepare a normal task prompt', acceptsText: true },
  { name: 'stop', description: 'Request cancellation of the active task' },
  { name: 'clear', description: 'Start a fresh conversation; keep saved history' },
  { name: 'new', description: 'Start a new conversation' },
  { name: 'tasks', description: 'Open task history' },
  { name: 'help', description: 'Show available slash commands' },
] as const
export type SlashName = typeof slashCommands[number]['name']

export function parseSlashCommand(text: string): { name: SlashName | null; argument: string } | null {
  const match = text.trim().match(/^\/([a-z][a-z-]*)(?:\s+([\s\S]*))?$/i)
  if (!match) return null
  const name = match[1].toLowerCase()
  return { name: slashCommands.some(item => item.name === name) ? name as SlashName : null,
           argument: match[2]?.trim() || '' }
}

export function matchingSlashCommands(text: string) {
  if (!/^\/[a-z-]*$/i.test(text)) return []
  const query = text.slice(1).toLowerCase()
  return slashCommands.filter(item => item.name.startsWith(query))
}
