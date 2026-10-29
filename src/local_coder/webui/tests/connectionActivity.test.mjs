import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import { readFile, writeFile, mkdtemp, mkdir, rm } from 'node:fs/promises'
import { dirname, join, posix } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import ts from 'typescript'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { activityItems } from '../src/activityPresentation.ts'
import { classifierStatusLabel } from '../src/routingStatus.ts'

await mkdir(new URL('../node_modules/.tmp/', import.meta.url), { recursive: true })
const temporary = await mkdtemp(fileURLToPath(new URL('../node_modules/.tmp/connection-ui-', import.meta.url)))
after(() => rm(temporary, { recursive: true, force: true }))
const compiled = new Set()
async function compile(relative) {
  const target = join(temporary, relative.replace(/\.tsx?$/, '.mjs'))
  if (compiled.has(relative)) return target
  compiled.add(relative)
  const source = await readFile(new URL(`../src/${relative}`, import.meta.url), 'utf8')
  let output = ts.transpileModule(source, { compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2023 } }).outputText
    .replace(/^import ['"][^'"]+\.css['"];?\s*$/gm, '')
  for (const [, quote, specifier] of [...output.matchAll(/from (['"])(\.{1,2}\/[^'"]+)\1/g)]) {
    const base = posix.normalize(posix.join(posix.dirname(relative), specifier))
    let dependency
    for (const extension of /\.tsx?$/.test(base) ? [''] : ['.ts', '.tsx']) {
      try { await readFile(new URL(`../src/${base}${extension}`, import.meta.url)); dependency = base + extension; break }
      catch (error) { if (error.code !== 'ENOENT') throw error }
    }
    assert.ok(dependency, `Cannot resolve ${specifier} from ${relative}`)
    const dependencyTarget = await compile(dependency)
    output = output.replaceAll(`from ${quote}${specifier}${quote}`, `from ${JSON.stringify(pathToFileURL(dependencyTarget).href)}`)
  }
  await mkdir(dirname(target), { recursive: true })
  await writeFile(target, output)
  return target
}
async function component(name) { return import(pathToFileURL(await compile(`components/${name}.tsx`)).href) }
const event = (seq, event_type, message, source = 'CODER-1') => ({ seq, event_type, message, source, data: {} })

test('activity pairs adjacent matching tool events and retains their exact evidence', () => {
  const events = [event(1, 'tool_call', 'Calling read_file'), event(2, 'tool_result', 'read_file: succeeded')]
  const original = structuredClone(events)
  const items = activityItems(events)
  assert.equal(items.length, 1)
  assert.equal(items[0].label, 'Read a file')
  assert.equal(items[0].tone, 'success')
  assert.deepEqual(items[0].events, events)
  assert.deepEqual(events, original)
  assert.equal(activityItems([events[0], { ...events[1], source: 'CODER-2' }]).length, 2)
  assert.equal(activityItems([events[0], event(3, 'checkpoint_saved', 'Saved'), events[1]]).length, 3)
})

test('activity distinguishes failures, budgets and tool completion from verification', () => {
  const items = activityItems([
    event(1, 'tool_result', 'run_tests: failed'), event(2, 'budget_exhausted', 'Token budget reached'),
    event(3, 'tool_result', 'verify_ui: succeeded'), event(4, 'custom_event', 'Exact observed message'),
  ])
  assert.equal(items[0].tone, 'error')
  assert.equal(items[1].tone, 'warning')
  assert.equal(items[2].label, 'Finished the interface tool')
  assert.equal(items[2].tone, 'neutral')
  assert.doesNotMatch(items[2].label, /verified/i)
  assert.equal(items[3].label, 'Exact observed message')
})

test('classifier status never promotes unsupported or unknown configurations to ready', () => {
  assert.equal(classifierStatusLabel('unsupported'), 'Unavailable for this configuration')
  assert.equal(classifierStatusLabel('configured_unverified'), 'Configured; readiness unverified')
  assert.equal(classifierStatusLabel('ready'), 'Ready')
  assert.equal(classifierStatusLabel('not_configured'), 'Not configured')
  assert.equal(classifierStatusLabel('future-state'), 'Readiness unknown')
})

test('activity renders readable summaries and keeps internal details escaped and expandable', async () => {
  const { ActivityTimeline } = await component('ActivityTimeline')
  const html = renderToStaticMarkup(createElement(ActivityTimeline, { active: true, events: [
    event(1, 'tool_call', 'Calling read_file'), event(2, 'tool_result', 'read_file: succeeded'),
    event(3, 'custom', '<script>untrusted</script>', 'ORCHESTRATOR'),
  ] }))
  assert.match(html, /activity-timeline" open=""/)
  assert.match(html, /<summary>Read a file<span[^>]*aria-hidden="true"/)
  assert.match(html, /3 events/)
  assert.match(html, /&lt;script&gt;untrusted&lt;\/script&gt;/)
  assert.doesNotMatch(html, /<script>/)
  assert.match(html, /<details><summary>/)
  assert.doesNotMatch(html, /Calling read_file|ORCHESTRATOR|activity-event-detail/)
})

test('long activity diagnostics get a bounded Unicode-safe preview with recoverable originals', () => {
  const message = 'A'.repeat(239) + '🧪' + 'extra diagnostic '.repeat(500)
  const items = activityItems([event(1, 'custom', message)])
  assert.ok(items[0].label.length <= 241)
  assert.match(items[0].label, /…$/)
  assert.doesNotMatch(items[0].label, /[\uD800-\uDBFF]…$/)
  assert.equal(items[0].events[0].message, message)
})

test('long activity lists show recent steps with an explicit earlier-history control', async () => {
  const { ActivityTimeline } = await component('ActivityTimeline')
  const html = renderToStaticMarkup(createElement(ActivityTimeline, { events: Array.from({ length: 15 }, (_, i) => event(i, 'custom', `Recorded observation ${i}`)) }))
  assert.match(html, /Show 3 earlier steps/)
  assert.match(html, /Recorded observation 14/)
  assert.doesNotMatch(html, /Recorded observation 0</)
  assert.equal((html.match(/class="activity-step /g) || []).length, 12)
})

const models = [
  { id: 'vendor/model:free', name: 'Model Free', is_free: true, context_length: 8192, input_cost_per_million: 0, output_cost_per_million: 0, supports_tools: true },
  { id: 'vendor/model', name: 'Model Paid', is_free: false, context_length: 32768, input_cost_per_million: 1, output_cost_per_million: 2, supports_tools: false },
  { id: 'vendor/unknown', name: 'Model Unknown' },
]
test('catalog renders exact free and paid variants, honest unknowns and selection semantics', async () => {
  const { ModelCatalogPicker } = await component('ModelCatalogPicker')
  let selected = 0
  const html = renderToStaticMarkup(createElement(ModelCatalogPicker, { catalog: { models, source: 'fixture', stale: true }, selected: 'vendor/model:free', loading: false, choose: () => { selected += 1 } }))
  assert.match(html, /vendor\/model:free/)
  assert.match(html, /vendor\/model<\/code>/)
  assert.match(html, />Free<\/span>/)
  assert.match(html, />Metered<\/span>/)
  assert.match(html, />Price unknown<\/span>/)
  assert.match(html, /Context unknown/)
  assert.match(html, /Cached model metadata may be out of date/)
  assert.equal((html.match(/aria-pressed="true"/g) || []).length, 1)
  assert.match(html, /Selecting a model does not start a request/)
  assert.equal(selected, 0)
})

test('free-only catalog excludes metered and unknown-price variants', async () => {
  const { ModelCatalogPicker } = await component('ModelCatalogPicker')
  const html = renderToStaticMarkup(createElement(ModelCatalogPicker, { catalog: { models, source: 'fixture' }, selected: '', loading: false, initialFreeOnly: true, choose() {} }))
  assert.match(html, /Model Free/)
  assert.doesNotMatch(html, /Model Paid|Model Unknown/)
  assert.equal((html.match(/class="catalog-model"/g) || []).length, 1)
})

test('provider setup exposes masked credentials and a direct manual model field', async () => {
  const { ProviderSettings } = await component('ProviderSettings')
  const html = renderToStaticMarkup(createElement(ProviderSettings, { provider: { backend: 'anthropic', base_url: 'https://api.anthropic.com/v1', model_id: '', profile: 'generic', api_key_env: 'ANTHROPIC_API_KEY' }, close() {}, saved() {} }))
  assert.match(html, /aria-labelledby="settings-title"/)
  assert.match(html, /Model connections/)
  assert.match(html, /type="password"[^>]*autoComplete="off"/i)
  assert.match(html, /Environment variable name/)
  assert.match(html, /stores its name, never its value/)
  assert.match(html, /<label>Model ID<input required=""/)
  assert.ok(html.indexOf('<label>Model ID') < html.indexOf('Advanced settings and manual overrides'))
  assert.match(html, /Saving does not start a model request/)
  assert.match(html, /Save connection/)
})
