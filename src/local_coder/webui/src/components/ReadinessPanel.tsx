type Gate = { status: string; missing_gates?: string[]; evidence?: unknown }
export type Readiness = {
  ready: boolean; revision: string | null; spec_version: string; limitation: string
  items: { id: number; title: string; acceptance: string; status: string; evidence: { path: string; status: string; meaning: string }[]; missing_gates: string[] }[]
  commercial: Record<string, Gate>; native_runs: Record<string, Gate>; model_benchmark: Gate; missing_external_gates: string[]
}

export function ReadinessPanel({ readiness }: { readiness: Readiness | undefined }) {
  return <details className="section-card"><summary>Release readiness evidence</summary>
    {!readiness ? <p>Readiness evidence is unavailable from this engine.</p> : <>
      <p>{readiness.ready ? 'Recorded readiness gates satisfied' : 'Readiness remains unverified or has missing gates'} · Revision {readiness.revision || 'Unavailable'} · Specification {readiness.spec_version}</p>
      <p className="hint">{readiness.limitation}</p>
      {readiness.items.map(item => <details key={item.id}><summary>{item.id}. {item.title} · {item.status}</summary><p>{item.acceptance}</p><ul>{item.evidence.map((entry, index) => <li key={index}><code>{entry.path}</code> · {entry.status} · {entry.meaning}</li>)}</ul>{item.missing_gates.map(gate => <p className="hint" key={gate}>{gate}</p>)}</details>)}
      <h3>External release gates</h3>
      {[...Object.entries(readiness.commercial), ...Object.entries(readiness.native_runs), ['model_benchmark', readiness.model_benchmark] as const].map(([name, gate]) => <p key={name}>{name.replaceAll('_', ' ')}: {gate.status}{gate.missing_gates?.length ? ` · ${gate.missing_gates.join('; ')}` : ''}</p>)}
      <ul>{readiness.missing_external_gates.map(gate => <li key={gate}>{gate}</li>)}</ul>
    </>}
  </details>
}
