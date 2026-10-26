import { useEffect, useState } from 'react'
import { api } from '../api'
type Playbook = { id: string; title: string; steps: string[]; evidence_required: string[]; verification: string }
export function SecurityPlaybooks() {
  const [items, setItems] = useState<Playbook[]>([]), [error, setError] = useState('')
  useEffect(() => { let active = true; void api<{ playbooks: Playbook[] }>('/security-playbooks').then(result => { if (active) setItems(result.playbooks) }).catch(error => { if (active) setError(String(error)) }); return () => { active = false } }, [])
  return <section className="playbook-view"><h1>Security playbooks</h1><p>Review checklists used by the security and red team agents. Each includes refutation steps and evidence requirements.</p>{error && <p role="alert">{error}</p>}{items.map(item => <details key={item.id}><summary>{item.title}</summary><ol>{item.steps.map(step => <li key={step}>{step}</li>)}</ol><strong>Evidence required</strong><ul>{item.evidence_required.map(step => <li key={step}>{step}</li>)}</ul><p className="hint">{item.verification}</p></details>)}</section>
}
