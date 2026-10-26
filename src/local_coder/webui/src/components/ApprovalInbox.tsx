import { useEffect, useRef, useState } from 'react'
import { api } from '../api'

type Approval = { id: string; description: string; status: string; created_at: number }

export function ApprovalInbox({ connected = true }: { connected?: boolean }) {
  const [items, setItems] = useState<Approval[]>([])
  const [answering, setAnswering] = useState<string[]>([])
  const [error, setError] = useState('')
  const [decisionError, setDecisionError] = useState('')
  const answered = useRef(new Set<string>())
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    let stopped = false
    let timer: ReturnType<typeof setTimeout>
    async function poll() {
      if (!connected) return
      try {
        const result = await api<{ approvals: Approval[] }>('/approvals')
        if (stopped) return
        const pending = result.approvals.filter(item => item.status === 'pending' && !answered.current.has(item.id))
        setItems(pending)
        setError('')
      } catch (cause) {
        if (!stopped) { setItems([]); setError(cause instanceof Error ? cause.message : 'Cannot load pending approvals') }
      } finally {
        if (!stopped && connected) timer = setTimeout(() => { void poll() }, 1200)
      }
    }
    void poll()
    return () => { stopped = true; mounted.current = false; clearTimeout(timer) }
  }, [connected])

  async function respond(item: Approval, approved: boolean) {
    if (!connected || answering.includes(item.id)) return
    setAnswering(current => [...current, item.id]); setDecisionError('')
    try {
      await api(`/approvals/${encodeURIComponent(item.id)}/respond`, 'POST', { approved })
      answered.current.add(item.id)
      if (mounted.current) setItems(current => current.filter(request => request.id !== item.id))
    } catch (cause) {
      if (mounted.current) setDecisionError(cause instanceof Error ? cause.message : 'Cannot submit this decision')
    } finally {
      if (mounted.current) setAnswering(current => current.filter(id => id !== item.id))
    }
  }

  if (!connected || (!items.length && !error && !decisionError)) return null
  return <aside className="approval-inbox" aria-labelledby="approval-inbox-title">
    <header><h2 id="approval-inbox-title">{items.length ? 'Human approval needed' : 'Approval inbox unavailable'}</h2>{items.length > 0 && <span className="hint">{items.length} pending</span>}</header>
    {error && <p role="alert" className="error-text">{error}</p>}
    {decisionError && <p role="alert" className="error-text">{decisionError}</p>}
    {items.length > 0 && <p className="hint">Review the action below. Each decision applies only to that request.</p>}
    <div aria-live="polite">
      {items.map(item => <section className="approval-request" key={item.id} aria-label={`Approval request ${item.id.slice(0, 8)}`}>
        <p className="hint">Requested {new Date(item.created_at * 1000).toLocaleTimeString()} · {item.id.slice(0, 8)}</p>
        <pre tabIndex={0}>{item.description}</pre>
        <div className="approval-actions">
          <button type="button" disabled={answering.includes(item.id)} onClick={() => { void respond(item, false) }}>Deny</button>
          <button type="button" className="primary" disabled={answering.includes(item.id)} onClick={() => { void respond(item, true) }}>{answering.includes(item.id) ? 'Submitting…' : 'Allow this request'}</button>
        </div>
      </section>)}
    </div>
  </aside>
}
