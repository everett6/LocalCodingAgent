import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import { readFile, writeFile, mkdtemp, mkdir, rm } from 'node:fs/promises'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { join } from 'node:path'
import ts from 'typescript'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { inspectorTabForKey } from '../src/components/runInspectorTabs.ts'

// Compile only the reviewed JSX modules with the project's installed compiler.
await mkdir(new URL('../node_modules/.tmp/', import.meta.url), { recursive: true })
const temporary = await mkdtemp(fileURLToPath(new URL('../node_modules/.tmp/clin-ui-test-', import.meta.url)))
after(() => rm(temporary, { recursive: true, force: true }))
async function component(name) {
  const location = new URL(`../src/components/${name}.tsx`, import.meta.url)
  const source = await readFile(location, 'utf8')
  let output = ts.transpileModule(source, { compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2023 } }).outputText
  for (const helper of ['runInspectorTabs', 'reportBlocks']) {
    output = output.replaceAll(`'./${helper}'`, JSON.stringify(new URL(`../src/components/${helper}.ts`, import.meta.url).href))
  }
  output = output.replaceAll("'../product'", JSON.stringify(new URL('../src/product.ts', import.meta.url).href))
  const target = join(temporary, `${name}.mjs`)
  await writeFile(target, output)
  return import(pathToFileURL(target).href)
}

test('inspector tabs support the complete standard directional keyboard map', () => {
  assert.equal(inspectorTabForKey('overview', 'ArrowRight'), 'activity')
  assert.equal(inspectorTabForKey('artifacts', 'ArrowRight'), 'overview')
  assert.equal(inspectorTabForKey('overview', 'ArrowLeft'), 'artifacts')
  assert.equal(inspectorTabForKey('activity', 'Home'), 'overview')
  assert.equal(inspectorTabForKey('overview', 'End'), 'artifacts')
  assert.equal(inspectorTabForKey('overview', 'Tab'), null)
})

test('rendered inspector has one tab stop and a uniquely linked accessible panel', async () => {
  const { RunInspector } = await component('RunInspector')
  const props = { session: { session_id: 'fixture', request: 'Review' }, tasks: [], events: [], workspace: null, context: null, onNavigate() {}, onClose() {} }
  const markup = renderToStaticMarkup(createElement('main', null, createElement(RunInspector, props), createElement(RunInspector, props)))
  const tabs = markup.match(/<button[^>]*role="tab"[^>]*>/g)
  assert.equal(tabs.length, 6)
  assert.equal(tabs.filter(tab => tab.includes('tabindex="0"')).length, 2)
  assert.equal(tabs.filter(tab => tab.includes('tabindex="-1"')).length, 4)
  const ids = [...markup.matchAll(/\bid="([^"]+)"/g)].map(match => match[1])
  assert.equal(new Set(ids).size, ids.length)
  for (const [, labelledBy] of markup.matchAll(/role="tabpanel"[^>]*aria-labelledby="([^"]+)"/g)) assert.ok(ids.includes(labelledBy))
})

test('rendered report escapes HTML and has safe explicit links and accessible copy controls', async () => {
  const { ReportView } = await component('ReportView')
  const markup = renderToStaticMarkup(createElement(ReportView, { text: '# Findings\n\n<script>alert(1)</script>\n\n[bad](javascript:alert(1)) [docs](https://example.test/)\n\n```sh\necho safe\n```' }))
  assert.ok(markup.includes('<h1'))
  assert.ok(markup.includes('&lt;script&gt;'))
  assert.ok(!markup.includes('<script>'))
  assert.ok(!markup.includes('href="javascript:'))
  assert.ok(markup.includes('href="https://example.test/" target="_blank" rel="noopener noreferrer"'))
  assert.ok(markup.includes('aria-label="Copy sh code"'))
  assert.ok(markup.includes('Download full report'))
})
