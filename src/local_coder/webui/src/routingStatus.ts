export function classifierStatusLabel(state: string): string {
  const labels: Record<string, string> = {
    ready: 'Ready', configured_unverified: 'Configured; readiness unverified',
    unsupported: 'Unavailable for this configuration', not_configured: 'Not configured',
  }
  return labels[state] || 'Readiness unknown'
}
