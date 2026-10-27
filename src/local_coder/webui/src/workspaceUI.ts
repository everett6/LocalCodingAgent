import type { Mode } from './api'

export function isContextPath(path: string): boolean {
  if (!path || !/^[\p{L}\p{N}_./\\-]+$/u.test(path) || /^[\\/]/.test(path)) return false
  const parts = path.replaceAll('\\', '/').split('/')
  if (parts.some(part => ['..', '.git', '.local-coder', '.ssh', '.aws', 'secrets'].includes(part))) return false
  const name = (parts.at(-1) || '').toLowerCase()
  return !name.startsWith('.env') && !['id_rsa', 'id_ed25519', 'credentials.json', '.npmrc', '.netrc'].includes(name)
    && !/\.(pem|key|p12|pfx|crt)$/.test(name)
}

export function taskObjective(request: string, files: string[], mode: Mode): string {
  if (mode === 'red_team') return request
  const selected = [...new Set(files.filter(isContextPath))].slice(0, 12)
  return selected.length ? `${request}\n\nRelevant local files:\n${selected.map(path => `@${path}`).join('\n')}` : request
}

/** A new job must carry the reviewed plan; conversation selection is not model context. */
export function planDraft(objective: string, report: string): string {
  const limit = 60000
  const excerpt = report.slice(0, limit)
  const note = report.length > limit ? '\n\n[Plan excerpt truncated. Review the complete saved report before implementation.]' : ''
  return `Implement this reviewed plan. Verify its assumptions against the current workspace before editing.\n\nOriginal objective: ${objective.slice(0, 12000)}\n\nReviewed plan:\n${excerpt}${note}`
}

export type PlanTask = { task_id: string; objective: string; status?: string }
export function validPlanTasks(value: unknown): PlanTask[] {
  if (!Array.isArray(value)) return []
  return value.filter((task): task is PlanTask => !!task && typeof task === 'object' && typeof task.task_id === 'string' && typeof task.objective === 'string' && (task.status === undefined || typeof task.status === 'string')).slice(0, 200)
}
