import { useMemo, useState } from 'react'
import type { CatalogModel, ModelCatalog } from '../api'

const pageSize = 25
const controlStyle = { border: '1px solid #444', borderRadius: 5, padding: '7px 10px', fontSize: 11 } as const

function price(value: number | null | undefined): string {
  return value == null ? 'Unknown' : `$${value.toLocaleString(undefined, { maximumFractionDigits: 8 })}`
}

export function ModelCatalogPicker({ catalog, selected, loading, error, choose, refresh, disabled = false, initialFreeOnly = false }: {
  catalog: ModelCatalog | null
  selected: string
  loading: boolean
  error?: string
  choose: (model: CatalogModel) => void
  refresh?: () => void
  disabled?: boolean
  initialFreeOnly?: boolean
}) {
  const [query, setQuery] = useState('')
  const [freeOnly, setFreeOnly] = useState(initialFreeOnly)
  const [toolsOnly, setToolsOnly] = useState(false)
  const [page, setPage] = useState(0)
  const filtered = useMemo(() => {
    const search = query.trim().toLowerCase()
    return (catalog?.models || []).filter(model => (!freeOnly || model.is_free === true)
      && (!toolsOnly || model.supports_tools === true)
      && (!search || `${model.id} ${model.name}`.toLowerCase().includes(search)))
  }, [catalog, freeOnly, toolsOnly, query])
  const pages = Math.max(1, Math.ceil(filtered.length / pageSize))
  const currentPage = Math.min(page, pages - 1)
  const visible = filtered.slice(currentPage * pageSize, (currentPage + 1) * pageSize)
  const chosen = catalog?.models.find(model => model.id === selected)

  return <section className="model-catalog" aria-label="Model catalog" style={{ margin: '16px 0', minWidth: 0 }}>
    <label>Search all models<input value={query} placeholder="Model name, provider, NVIDIA, :free…" onChange={event => { setQuery(event.target.value); setPage(0) }} /></label>
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 14, alignItems: 'center', marginBottom: 12 }}>
      <label style={{ display: 'flex', flexDirection: 'row', alignItems: 'center', gap: 6, margin: 0 }}><input type="checkbox" checked={freeOnly} onChange={event => { setFreeOnly(event.target.checked); setPage(0) }} />Free only</label>
      <label style={{ display: 'flex', flexDirection: 'row', alignItems: 'center', gap: 6, margin: 0 }}><input type="checkbox" checked={toolsOnly} onChange={event => { setToolsOnly(event.target.checked); setPage(0) }} />Tool calling</label>
      {refresh && <button type="button" disabled={disabled || loading} style={controlStyle} onClick={refresh}>Refresh catalog</button>}
    </div>
    {loading && <p role="status" className="hint">Loading model metadata…</p>}
    {(error || catalog?.error) && <p role="alert" className="error-text">{error || catalog?.error}</p>}
    {catalog && <p className="hint">{filtered.length.toLocaleString()} matching · {catalog.models.length.toLocaleString()} models in {catalog.source}. {catalog.stale ? 'Cached metadata may be out of date.' : 'Metadata is supplied by the provider.'}</p>}
    {chosen && <p className="hint">Selected: <code>{chosen.id}</code> · {chosen.is_free === true ? 'Free according to catalog pricing' : chosen.is_free === false ? 'Metered model' : 'Pricing status unknown'}</p>}
    {!loading && catalog && !filtered.length && <p className="hint">No models match these filters. Try another search or clear the filters.</p>}
    <div style={{ maxHeight: 390, overflow: 'auto', border: '1px solid #3b3b3b', borderRadius: 6 }}>
      {visible.map(model => <button className="catalog-model" key={model.id} type="button" disabled={disabled} aria-pressed={model.id === selected} onClick={() => choose(model)} style={{ display: 'block', width: '100%', textAlign: 'left', padding: '12px', borderBottom: '1px solid #363636', background: model.id === selected ? '#343b48' : undefined }}>
        <span style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center', fontSize: 12 }}><strong>{model.name || model.id}</strong><span className={`status-badge ${model.is_free === true ? 'free' : ''}`} style={{ color: model.is_free === true ? 'var(--green)' : 'var(--muted)', fontSize: 10 }}>{model.is_free === true ? 'FREE' : model.is_free === false ? 'METERED' : 'PRICE UNKNOWN'}</span>{model.id.endsWith(':free') && <span className="hint">Exact :free variant</span>}{model.id === selected && <span style={{ color: 'var(--accent)' }}>Selected</span>}</span>
        <code style={{ display: 'block', margin: '5px 0', fontSize: 10, overflowWrap: 'anywhere' }}>{model.id}</code>
        <span className="hint" style={{ display: 'block' }}>Context: {model.context_length ? `${model.context_length.toLocaleString()} tokens` : 'Not reported'} · Output: {model.max_output_tokens ? `${model.max_output_tokens.toLocaleString()} tokens` : 'Not reported'} · Tools: {model.supports_tools === true ? 'Yes' : model.supports_tools === false ? 'No' : 'Unknown'}</span>
        <span className="hint" style={{ display: 'block' }}>USD / million tokens: input {price(model.input_cost_per_million)} · output {price(model.output_cost_per_million)}{model.request_cost_usd != null && model.request_cost_usd !== 0 ? ` · ${price(model.request_cost_usd)} / request` : ''}</span>
      </button>)}
    </div>
    {filtered.length > pageSize && <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10, marginTop: 9 }}><button type="button" style={controlStyle} disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)}>Previous</button><span className="hint">Page {currentPage + 1} of {pages} · {currentPage * pageSize + 1}–{Math.min((currentPage + 1) * pageSize, filtered.length)}</span><button type="button" style={controlStyle} disabled={currentPage >= pages - 1} onClick={() => setPage(currentPage + 1)}>Next</button></div>}
    <p className="hint">Free labels follow the exact model ID and listed prices. Paid and :free variants are separate choices. Selecting a model never starts a model request.</p>
  </section>
}
