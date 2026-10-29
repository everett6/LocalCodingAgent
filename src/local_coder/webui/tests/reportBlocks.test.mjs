import test from 'node:test'
import assert from 'node:assert/strict'
import { parseInline, parseReport, safeReportUrl } from '../src/components/reportBlocks.ts'

test('reports become readable semantic sections, lists, quotes and literal code', () => {
  const report = parseReport('# Review\n\nObserved **unsafe input**.\n\n- Check caller\n- Inspect guard\n\n> Unverified candidate\n\n```python\nprint("<tag>")\n```\n\n---')
  assert.deepEqual(report.blocks.map(block => block.kind), ['heading', 'paragraph', 'list', 'quote', 'code', 'rule'])
  assert.equal(report.blocks[0].level, 1)
  assert.deepEqual(report.blocks[2].items, ['Check caller', 'Inspect guard'])
  assert.equal(report.blocks[4].language, 'python')
  assert.equal(report.blocks[4].text, 'print("<tag>")')
  assert.equal(report.truncated, false)
})

test('HTML and unsafe URL syntax remain text; only explicit safe web URLs become links', () => {
  const html = '<script>alert(1)</script><img src=x onerror=alert(2)>'
  assert.deepEqual(parseInline(html), [{ kind: 'text', text: html }])
  for (const url of ['javascript:alert(1)', 'data:text/html,hello', 'file:///etc/passwd', 'codex://run', '//evil.test', 'https://user:password@example.test', 'https://good.test\\@evil.test', 'https://good.test/\u0000']) {
    assert.equal(safeReportUrl(url), null)
    const markdown = `[label](${url})`
    assert.equal(parseInline(markdown).map(item => item.text).join(''), markdown)
  }
  const valid = parseInline('[Documentation](https://example.test/page) and `x < y` with **care**')
  assert.equal(valid[0].kind, 'link')
  assert.equal(valid[0].href, 'https://example.test/page')
  assert.ok(valid.some(item => item.kind === 'code' && item.text === 'x < y'))
  assert.ok(valid.some(item => item.kind === 'strong' && item.text === 'care'))
})

test('fences preserve markup literally and tolerate missing or longer closing fences', () => {
  const open = parseReport('~~~sh\n# literal heading\n[link](javascript:bad)\n')
  assert.equal(open.blocks.length, 1)
  assert.equal(open.blocks[0].kind, 'code')
  assert.equal(open.blocks[0].text, '# literal heading\n[link](javascript:bad)\n')
  const closed = parseReport('````typescript\n```\nconst n = 1\n````\nAfter')
  assert.equal(closed.blocks[0].text, '```\nconst n = 1')
  assert.equal(closed.blocks[1].text, 'After')
})

test('Unicode preview respects UTF-8 and character limits without breaking code points', () => {
  const original = '😀'.repeat(40000)
  const result = parseReport(original)
  assert.equal(result.truncated, true)
  assert.ok(new TextEncoder().encode(result.previewText).byteLength <= 120000)
  assert.ok(result.previewText.length <= 120000)
  assert.equal([...result.previewText].at(-1), '😀')
  assert.equal(result.previewText, '😀'.repeat(30000))
})

test('many list items and table cells consume the block budget and disclose truncation', () => {
  const list = parseReport(Array.from({ length: 2500 }, (_, index) => `- evidence ${index}`).join('\n'))
  assert.equal(list.truncated, true)
  assert.ok(list.blockCount <= 1500)
  assert.ok(list.blocks[0].items.length < 1500)
  const table = parseReport('| Asset | Status |\n| --- | --- |\n| app.py | unknown |')
  assert.equal(table.blocks[0].kind, 'table')
  assert.deepEqual(table.blocks[0].headers, ['Asset', 'Status'])
  assert.deepEqual(table.blocks[0].rows, [['app.py', 'unknown']])
})

test('unsupported and malformed formatting preserves its visible text', () => {
  for (const text of ['unfinished **strong', 'unclosed `code', '[not a link', '![alt](https://example.test/image.png)', '<b>literal HTML</b>']) {
    assert.equal(parseInline(text).map(item => item.text).join(''), text)
  }
  const repeated = ('**x** ').repeat(5000)
  const bounded = parseInline(repeated)
  assert.ok(bounded.length < 300)
  assert.equal(bounded[0].kind, 'strong')
  assert.ok(bounded.at(-1).text.endsWith('**x** '), 'unformatted tail must remain visible')
})


test('large ASCII reports and large tables have bounded previews and render units', () => {
  const ascii = parseReport('a'.repeat(150000))
  assert.equal(ascii.previewText.length, 120000)
  assert.equal(ascii.truncated, true)
  const table = parseReport('| Asset | Status |\n| --- | --- |\n' + '| app.py | unknown |\n'.repeat(2000))
  assert.equal(table.truncated, true)
  assert.ok(table.blockCount <= 1500)
  assert.ok(table.blocks[0].rows.length < 1000)
})
