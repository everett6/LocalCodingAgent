import { useMemo, useState } from 'react'
import { Check, RefreshCw, Search } from 'lucide-react'
import type { CatalogModel, ModelCatalog } from '../api'

const pageSize = 25

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

  return <section className="model-catalog" aria-label="Model catalog" aria-busy={loading}>
    <div className="catalog-heading"><h3>Choose a model</h3>{refresh && <button type="button" className="connection-secondary" disabled={disabled || loading} onClick={refresh}><RefreshCw size={13}/>Refresh catalog</button>}</div>
    <label className="catalog-search"><span className="sr-only">Search all models</span><Search size={16}/><input value={query} placeholder="Search model names or providers…" onChange={event => { setQuery(event.target.value); setPage(0) }} /></label>
    <div className="catalog-filters">
      <label><input type="checkbox" checked={freeOnly} onChange={event => { setFreeOnly(event.target.checked); setPage(0) }} />Free only</label>
      <label><input type="checkbox" checked={toolsOnly} onChange={event => { setToolsOnly(event.target.checked); setPage(0) }} />Tool calling</label>
      {catalog && <span>{filtered.length.toLocaleString()} of {catalog.models.length.toLocaleString()} models</span>}
    </div>
    {loading && <p role="status" className="hint">Loading model metadata…</p>}
    {(error || catalog?.error) && <p role="alert" className="error-text">{error || catalog?.error}</p>}
    {catalog?.stale && <p className="catalog-stale" role="status">Cached model metadata may be out of date. Refresh for current availability and pricing.</p>}
    {!loading && catalog && !filtered.length && <div className="catalog-empty"><Search size={23}/><strong>No matching models</strong><p>Try another name, or turn off a filter.</p><button type="button" className="connection-secondary" onClick={() => { setQuery(''); setFreeOnly(false); setToolsOnly(false); setPage(0) }}>Clear filters</button></div>}
    {visible.length > 0 && <div className="catalog-results" aria-label="Available models">
      {visible.map(model => <button className="catalog-model" key={model.id} type="button" disabled={disabled} aria-pressed={model.id === selected} onClick={() => choose(model)}>
        <span className="catalog-choice-mark" aria-hidden="true">{model.id === selected && <Check size={12}/>}</span>
        <span className="catalog-model-copy"><span className="catalog-model-name"><strong>{model.name || model.id}</strong><span className={`catalog-price-badge ${model.is_free === true ? 'free' : ''}`}>{model.is_free === true ? 'Free' : model.is_free === false ? 'Metered' : 'Price unknown'}</span></span>
        <code>{model.id}</code>
        <span className="catalog-model-facts"><span>{model.context_length ? `${model.context_length.toLocaleString()} context` : 'Context unknown'}</span><span>{model.max_output_tokens ? `${model.max_output_tokens.toLocaleString()} output` : 'Output unknown'}</span><span>{model.supports_tools === true ? 'Tool calling' : model.supports_tools === false ? 'No tools' : 'Tools unknown'}</span></span>
        <span className="catalog-model-price">Per million tokens: {price(model.input_cost_per_million)} input / {price(model.output_cost_per_million)} output{model.request_cost_usd != null && model.request_cost_usd !== 0 ? ` + ${price(model.request_cost_usd)} per request` : ''}</span></span>
        {model.id === selected && <span className="sr-only">Selected</span>}
      </button>)}
    </div>}
    {filtered.length > pageSize && <div className="catalog-pagination"><button type="button" className="connection-secondary" disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)}>Previous</button><span>Page {currentPage + 1} of {pages}<small>{currentPage * pageSize + 1}–{Math.min((currentPage + 1) * pageSize, filtered.length)}</small></span><button type="button" className="connection-secondary" disabled={currentPage >= pages - 1} onClick={() => setPage(currentPage + 1)}>Next</button></div>}
    <p className="hint catalog-pricing-note">Free labels use provider prices for the exact model ID. Paid and :free variants are separate choices. Selecting a model does not start a request.</p>
  </section>
}
