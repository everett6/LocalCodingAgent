/** A deliberately small Markdown subset. Every value remains plain React text. */
export type ReportInline = { kind: 'text' | 'code' | 'strong' | 'link'; text: string; href?: string }
export type ReportBlock =
  | { kind: 'heading'; level: 1 | 2 | 3 | 4; text: string }
  | { kind: 'paragraph' | 'quote'; text: string }
  | { kind: 'code'; text: string; language: string }
  | { kind: 'list'; ordered: boolean; start: number; items: string[] }
  | { kind: 'table'; headers: string[]; rows: string[][] }
  | { kind: 'rule' }
export type ParsedReport = { blocks: ReportBlock[]; truncated: boolean; previewText: string; blockCount: number }
const MAX_TEXT = 120000
const MAX_BLOCKS = 1500

export function safeReportUrl(value: string): string | null {
  if (!value || value.length > 2048 || /[\s\\]/.test(value) || [...value].some(character => character.charCodeAt(0) < 32 || character.charCodeAt(0) === 127)) return null
  try {
    const url = new URL(value)
    if (!['http:', 'https:'].includes(url.protocol) || !url.hostname || url.username || url.password) return null
    return url.href
  } catch { return null }
}

export function parseInline(text: string): ReportInline[] {
  const result: ReportInline[] = []
  let index = 0
  let plain = 0
  let formatted = 0
  function literal(end: number) {
    if (end <= plain) return
    const value = text.slice(plain, end)
    const previous = result.at(-1)
    if (previous?.kind === 'text') previous.text += value
    else result.push({ kind: 'text', text: value })
  }
  while (index < text.length && formatted < 128) {
    let end = -1
    let token: ReportInline | null = null
    if (text[index] === '`') {
      const close = text.indexOf('`', index + 1)
      if (close < 0) break
      end = close + 1
      token = { kind: 'code', text: text.slice(index + 1, close) }
    } else if (text.startsWith('**', index)) {
      const close = text.indexOf('**', index + 2)
      if (close < 0) break
      end = close + 2
      token = { kind: 'strong', text: text.slice(index + 2, close) }
    } else if (text[index] === '[' && text[index - 1] !== '!') {
      const labelEnd = text.indexOf('](', index + 1)
      if (labelEnd < 0) break
      const close = text.indexOf(')', labelEnd + 2)
      if (close < 0) break
      const href = safeReportUrl(text.slice(labelEnd + 2, close))
      if (href) {
        end = close + 1
        token = { kind: 'link', text: text.slice(index + 1, labelEnd), href }
      } else {
        // Keep the complete rejected syntax, including its URL, as literal text.
        index = close + 1
        continue
      }
    }
    if (token) {
      literal(index)
      result.push(token)
      formatted += 1
      index = end
      plain = end
    } else index += 1
  }
  literal(text.length)
  return result
}

function preview(text: string): string {
  const parts: string[] = []
  let bytes = 0
  let characters = 0
  for (const point of text) {
    const code = point.codePointAt(0) ?? 0
    const width = code <= 0x7f ? 1 : code <= 0x7ff ? 2 : code <= 0xffff ? 3 : 4
    if (bytes + width > MAX_TEXT || characters + point.length > MAX_TEXT) break
    parts.push(point)
    bytes += width
    characters += point.length
  }
  return parts.join('')
}

const heading = (line: string) => /^ {0,3}(#{1,4})\s+(.+)$/.exec(line)
const fence = (line: string) => /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(line)
const listItem = (line: string) => /^ {0,3}([-+*]|\d{1,9}[.)])\s+(.*)$/.exec(line)
const quote = (line: string) => /^ {0,3}> ?(.*)$/.exec(line)
const rule = (line: string) => /^ {0,3}(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)
function cells(line: string): string[] {
  let value = line.trim()
  if (value.startsWith('|')) value = value.slice(1)
  if (value.endsWith('|')) value = value.slice(0, -1)
  return value.split('|').map(cell => cell.trim())
}
function tableHeader(lines: string[], index: number): string[] | null {
  if (!lines[index]?.includes('|') || !lines[index + 1]?.includes('|')) return null
  const headers = cells(lines[index])
  const separators = cells(lines[index + 1])
  return headers.length >= 2 && headers.length <= 20 && headers.length === separators.length
    && separators.every(cell => /^:?-{3,}:?$/.test(cell)) ? headers : null
}
function special(lines: string[], index: number): boolean {
  const line = lines[index]
  return !!heading(line) || !!fence(line) || !!listItem(line) || !!quote(line) || rule(line) || !!tableHeader(lines, index)
}

export function parseReport(text: string): ParsedReport {
  const previewText = preview(text)
  const lines = previewText.replace(/\r\n?/g, '\n').split('\n')
  const blocks: ReportBlock[] = []
  let index = 0
  let blockCount = 0
  let truncated = previewText.length < text.length
  function reserve(count = 1): boolean {
    if (blockCount + count > MAX_BLOCKS) { truncated = true; return false }
    blockCount += count
    return true
  }
  while (index < lines.length) {
    if (!lines[index].trim()) { index += 1; continue }
    if (blockCount >= MAX_BLOCKS) { truncated = true; break }
    const line = lines[index]
    const title = heading(line)
    const opening = fence(line)
    const item = listItem(line)
    const citation = quote(line)
    const headers = tableHeader(lines, index)
    if (title) {
      reserve()
      blocks.push({ kind: 'heading', level: title[1].length as 1 | 2 | 3 | 4, text: title[2] })
      index += 1
    } else if (opening) {
      reserve()
      const content: string[] = []
      index += 1
      while (index < lines.length) {
        const closing = /^ {0,3}(`+|~+)\s*$/.exec(lines[index])
        if (closing && closing[1][0] === opening[1][0] && closing[1].length >= opening[1].length) { index += 1; break }
        content.push(lines[index++])
      }
      const label = opening[2].trim().split(/\s+/)[0]
      blocks.push({ kind: 'code', text: content.join('\n'), language: /^[a-zA-Z0-9_+.#-]{1,40}$/.test(label) ? label : 'text' })
    } else if (rule(line)) {
      reserve(); blocks.push({ kind: 'rule' }); index += 1
    } else if (headers) {
      if (!reserve(1 + headers.length)) break
      const rows: string[][] = []
      let exhausted = false
      index += 2
      while (index < lines.length && lines[index].includes('|') && lines[index].trim()) {
        const row = cells(lines[index])
        // Unsupported column counts stay visible as an ordinary following block.
        if (row.length !== headers.length) break
        if (!reserve(1 + row.length)) { exhausted = true; break }
        rows.push(row); index += 1
      }
      blocks.push({ kind: 'table', headers, rows })
      if (exhausted) break
    } else if (item) {
      if (!reserve(2)) break
      const ordered = /^\d/.test(item[1])
      const items = [item[2]]
      const start = ordered ? Number.parseInt(item[1], 10) : 1
      index += 1
      while (index < lines.length) {
        const next = listItem(lines[index])
        if (!next || /^\d/.test(next[1]) !== ordered) break
        if (!reserve()) break
        items.push(next[2]); index += 1
      }
      blocks.push({ kind: 'list', ordered, start, items })
    } else if (citation) {
      reserve()
      const content = [citation[1]]
      index += 1
      while (index < lines.length) {
        const next = quote(lines[index])
        if (!next) break
        content.push(next[1]); index += 1
      }
      blocks.push({ kind: 'quote', text: content.join('\n') })
    } else {
      reserve()
      const content = [line]
      index += 1
      while (index < lines.length && lines[index].trim() && !special(lines, index)) content.push(lines[index++])
      blocks.push({ kind: 'paragraph', text: content.join('\n') })
    }
  }
  return { blocks, truncated, previewText, blockCount }
}
