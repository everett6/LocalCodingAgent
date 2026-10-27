import { useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { parseInline, parseReport } from './reportBlocks'
import type { ReportBlock } from './reportBlocks'
import { PRODUCT } from '../product'

function inline(text: string): ReactNode[] {
  return parseInline(text).map((item, index) => {
    if (item.kind === 'code') return <code key={index}>{item.text}</code>
    if (item.kind === 'strong') return <strong key={index}>{item.text}</strong>
    if (item.kind === 'link') return <a key={index} href={item.href} target="_blank" rel="noopener noreferrer">{item.text}</a>
    return item.text
  })
}

function CodeBlock({ text, language }: { text: string; language: string }) {
  const [status, setStatus] = useState<{ message: string; failed: boolean } | null>(null)
  const [copying, setCopying] = useState(false)
  async function copy() {
    setCopying(true)
    setStatus(null)
    try {
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable')
      await navigator.clipboard.writeText(text)
      setStatus({ message: 'Code copied.', failed: false })
    } catch {
      setStatus({ message: 'Could not copy code. Select the code and copy it manually.', failed: true })
    } finally { setCopying(false) }
  }
  return <section className="report-code" aria-label={`${language} code block`}>
    <div className="code-toolbar">
      <span>{language}</span>
      <button type="button" disabled={copying} onClick={() => { void copy() }} aria-label={`Copy ${language} code`}>{copying ? 'Copying…' : 'Copy code'}</button>
    </div>
    <pre tabIndex={0}><code>{text}</code></pre>
    {status && <p className="report-status" role={status.failed ? 'alert' : 'status'}>{status.message}</p>}
  </section>
}

function renderBlock(block: ReportBlock, index: number): ReactNode {
  switch (block.kind) {
    case 'heading': {
      const Tag = `h${block.level}` as 'h1' | 'h2' | 'h3' | 'h4'
      return <Tag className={`report-heading report-heading-${block.level}`} key={index}>{inline(block.text)}</Tag>
    }
    case 'paragraph': return <p className="report-paragraph" key={index}>{inline(block.text)}</p>
    case 'quote': return <blockquote className="report-quote report-callout" key={index}><p>{inline(block.text)}</p></blockquote>
    case 'rule': return <hr className="report-rule" key={index} />
    case 'code': return <CodeBlock key={`${index}:${block.language}:${block.text}`} text={block.text} language={block.language} />
    case 'list': {
      const items = block.items.map((text, item) => <li key={item}>{inline(text)}</li>)
      return block.ordered
        ? <ol className="report-list" start={block.start} key={index}>{items}</ol>
        : <ul className="report-list" key={index}>{items}</ul>
    }
    case 'table': return <div className="report-table-scroll" key={index} tabIndex={0} role="region" aria-label="Report table">
      <table className="report-table">
        <thead><tr>{block.headers.map((cell, column) => <th scope="col" key={column}>{inline(cell)}</th>)}</tr></thead>
        <tbody>{block.rows.map((row, line) => <tr key={line}>{row.map((cell, column) => <td key={column}>{inline(cell)}</td>)}</tr>)}</tbody>
      </table>
    </div>
  }
}

export function ReportView({ text }: { text: string }) {
  const report = useMemo(() => parseReport(text), [text])
  const [downloadError, setDownloadError] = useState('')
  const [downloadStatus, setDownloadStatus] = useState('')
  const [saving, setSaving] = useState(false)
  async function download() {
    setDownloadError('')
    setDownloadStatus('')
    if (window.localCoder) {
      setSaving(true)
      try {
        if (!window.localCoder.saveReport) throw new Error('Native report export is unavailable. Update and reopen the desktop app.')
        const result = await window.localCoder.saveReport(text, `${PRODUCT.name.toLowerCase()}-report.md`)
        if (result.saved) setDownloadStatus('Report saved.')
      } catch (error) { setDownloadError(error instanceof Error ? error.message : 'Could not save the report.') }
      finally { setSaving(false) }
      return
    }
    let url: string | null = null
    try {
      // Create the complete artifact only in response to this explicit action.
      url = URL.createObjectURL(new Blob([text], { type: 'text/markdown;charset=utf-8' }))
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = `${PRODUCT.name.toLowerCase().replace(/[^a-z0-9-]+/g, '-')}-report.md`
      document.body.appendChild(anchor)
      try { anchor.click() } finally { anchor.remove() }
      const pending = url
      window.setTimeout(() => URL.revokeObjectURL(pending), 1000)
      url = null
    } catch {
      if (url) URL.revokeObjectURL(url)
      setDownloadError('Could not download the report. Try again using a browser that supports local file downloads.')
    }
  }
  return <article className="report-document" aria-label="Task report">
    <div className="report-actions"><button type="button" disabled={saving} onClick={() => { void download() }}>{saving ? 'Saving…' : 'Download full report'}</button></div>
    {downloadError && <p className="report-status" role="alert">{downloadError}</p>}
    {downloadStatus && <p className="report-status" role="status">{downloadStatus}</p>}
    {report.truncated && <p className="report-callout report-truncated" role="status">
      Report preview truncated at 120,000 UTF-8 bytes or characters, or 1,500 content blocks. Download the full report to read all content.
    </p>}
    {report.blocks.map(renderBlock)}
    {!text && <p className="report-paragraph">No report content yet.</p>}
  </article>
}
