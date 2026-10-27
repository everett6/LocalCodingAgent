export type Command = {
  id: string
  title: string
  description?: string
  group: string
  keywords?: string[]
  run: () => void
  disabled?: boolean
}

function normalize(value: string): string {
  return value.normalize('NFKD').replace(/\p{M}/gu, '').toLowerCase().trim().replace(/\s+/g, ' ')
}

/** Search local command metadata only. Equal matches retain curated input order. */
export function filterCommands(commands: Command[], query: string): Command[] {
  const phrase = normalize(query)
  if (!phrase) return commands.slice(0, 50)
  const words = phrase.split(' ')
  return commands.map((command, index) => {
    const title = normalize(command.title)
    const supporting = normalize([command.description ?? '', ...(command.keywords ?? [])].join(' '))
    const searchable = `${title} ${supporting}`
    if (!words.every(word => searchable.includes(word))) return null
    const score = title === phrase ? 400 : title.startsWith(phrase) ? 300 : title.includes(phrase) ? 200
      : words.every(word => title.includes(word)) ? 100 : 0
    return { command, index, score }
  }).filter((match): match is { command: Command; index: number; score: number } => match !== null)
    .sort((left, right) => right.score - left.score || left.index - right.index)
    .slice(0, 50).map(match => match.command)
}

/** Move within displayed results, wrapping and never selecting disabled actions. */
export function nextCommandIndex(commands: Command[], current: number, direction: 1 | -1): number {
  if (!commands.length) return -1
  const start = current >= 0 && current < commands.length ? current : direction === 1 ? -1 : 0
  for (let step = 1; step <= commands.length; step++) {
    const index = ((start + direction * step) % commands.length + commands.length) % commands.length
    if (!commands[index].disabled) return index
  }
  return -1
}
