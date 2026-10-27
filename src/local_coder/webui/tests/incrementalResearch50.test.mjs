import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import { readFile, writeFile, mkdtemp, mkdir, rm } from 'node:fs/promises'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { join } from 'node:path'
import ts from 'typescript'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { workspaceNeeds, stateLabel } from '../src/workspaceUI.ts'
import { taskStatusLabel } from '../src/taskInbox.ts'

await mkdir(new URL('../node_modules/.tmp/', import.meta.url), { recursive: true })
const temporary = await mkdtemp(fileURLToPath(new URL('../node_modules/.tmp/research50-', import.meta.url)))
after(() => rm(temporary, { recursive: true, force: true }))
async function component(name) {
  const source = await readFile(new URL(`../src/components/${name}.tsx`, import.meta.url), 'utf8')
  const output = ts.transpileModule(source, { compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2023 } }).outputText
    .replaceAll("'../api'", JSON.stringify(new URL('../src/api.ts', import.meta.url).href))
  const target = join(temporary, `${name}.mjs`)
  await writeFile(target, output)
  return import(pathToFileURL(target).href)
}

test('idle and selected conversations request no workspace walk or diff', () => {
  for (const view of ['conversation', 'tasks', 'research', 'history']) assert.deepEqual(workspaceNeeds(view, null), { files: false, diff: false })
  assert.deepEqual(workspaceNeeds('conversation', 'context'), { files: true, diff: false })
  assert.deepEqual(workspaceNeeds('conversation', 'files'), { files: true, diff: false })
  assert.deepEqual(workspaceNeeds('workspace', null), { files: true, diff: true })
  assert.deepEqual(workspaceNeeds('workspace', null, true), { files: true, diff: false })
})

test('visible run state uses observed evidence and keeps unverified completion distinct', () => {
  const task = { session_id: 'x', request: 'Inspect', phase: 'run', status: 'completed' }
  assert.equal(stateLabel({ ...task, observed_state: 'completed_unverified' }), 'Completed · verification unrecorded')
  assert.equal(taskStatusLabel({ ...task, observed_state: 'verified' }), 'Verified')
  assert.equal(stateLabel({ ...task, observed_state: 'stalled' }), 'No recent activity')
  assert.equal(stateLabel({ ...task, observed_state: 'waiting' }), 'Waiting for approval')
})

test('rendered readiness shows missing gates without promoting present source to a pass', async () => {
  const { ReadinessPanel } = await component('ReadinessPanel')
  const markup = renderToStaticMarkup(createElement(ReadinessPanel, { readiness: { ready: false, revision: 'abc', spec_version: '1', limitation: 'Presence is not acceptance', items: [{ id: 16, title: 'Incremental UI', status: 'unverified', acceptance: 'Idle excludes diffs', evidence: [{ path: 'src/App.tsx', status: 'present', meaning: 'Source observed only' }], missing_gates: ['Native smoke missing'] }], commercial: {}, native_runs: {}, model_benchmark: { status: 'unverified', missing_gates: ['Benchmark missing'] }, missing_external_gates: ['Signing key unavailable'] } }))
  assert.match(markup, /Incremental UI · unverified/)
  assert.match(markup, /Presence is not acceptance/)
  assert.match(markup, /Native smoke missing/)
  assert.match(markup, /Signing key unavailable/)
})

test('rendered doctor and context attribution expose explicit inspection controls', async () => {
  const { SearchConnectionDoctor } = await component('SearchConnectionDoctor')
  const doctor = renderToStaticMarkup(createElement(SearchConnectionDoctor))
  assert.match(doctor, /never send a search query or enable a service/)
  assert.match(doctor, /Inspect saved search provenance/)
  const { ContextDebugger } = await component('ContextDebugger')
  const context = renderToStaticMarkup(createElement(ContextDebugger))
  assert.match(context, /Inspect context attribution/)
  assert.match(context, /serialized UTF-8 bytes/)
  assert.match(context, /Optional task ID/)
})
