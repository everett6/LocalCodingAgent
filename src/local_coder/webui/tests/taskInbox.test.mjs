import assert from 'node:assert/strict'
import test from 'node:test'
import { inboxCounts, selectInboxTasks, taskMatchesFilter, taskMatchesSearch, taskModeLabel, taskStatusLabel, taskTitle } from '../src/taskInbox.ts'

const session = (id, status, extra = {}) => ({ session_id: id, request: `Task ${id}`, phase: 'run', mode: 'coding', status, ...extra })

test('counts reflect real statuses, including recovery and quota states', () => {
  const tasks = ['queued', 'running', 'cancelling', 'blocked', 'quota_exhausted', 'interrupted', 'requires_continuation', 'failed', 'cancelled', 'completed', undefined, 'future_status'].map((status, i) => session(String(i), status))
  assert.deepEqual(inboxCounts(tasks), { all: 12, active: 3, attention: 6, completed: 1 })
  for (const status of ['blocked', 'quota_exhausted', 'interrupted', 'requires_continuation']) {
    assert.equal(taskMatchesFilter(session('recover', status), 'attention'), true)
    assert.equal(taskMatchesFilter(session('recover', status), 'completed'), false)
  }
  assert.equal(taskMatchesFilter(session('unknown', 'future_status'), 'attention'), false)
})

test('search covers requests, phase, mode, and raw or readable status', () => {
  const task = session('audit', 'quota_exhausted', { request: 'Review Unicode 中文 paths', phase: 'scan', mode: 'bug_hunting' })
  for (const search of ['unicode', 'SCAN', 'bug_hunting', 'bug hunting', 'quota_exhausted', 'Quota reached', '中文', ' paths   scan ']) {
    assert.equal(taskMatchesSearch(task, search), true, search)
  }
  assert.equal(taskMatchesSearch(task, 'unrelated'), false)
  assert.equal(taskMatchesSearch(task, '  '), true)
})

test('search counts and status filters use the same matching tasks', () => {
  const tasks = [session('one', 'running', { request: 'Fix parser' }), session('two', 'failed', { request: 'Fix indexing' }), session('three', 'completed', { request: 'Explain settings' })]
  assert.deepEqual(inboxCounts(tasks, 'fix'), { all: 2, active: 1, attention: 1, completed: 0 })
  assert.deepEqual(selectInboxTasks(tasks, [], 'fix', 'attention').map(task => task.session_id), ['two'])
  assert.deepEqual(selectInboxTasks(tasks, [], 'fix', 'completed'), [])
})

test('pinned tasks come first while both groups keep the original order', () => {
  const tasks = [session('a', 'completed'), session('b', 'running'), session('c', 'failed'), session('d', 'queued')]
  const original = [...tasks]
  assert.deepEqual(selectInboxTasks(tasks, ['d', 'b', 'missing', 'd']).map(task => task.session_id), ['b', 'd', 'a', 'c'])
  assert.deepEqual(selectInboxTasks(tasks, ['d', 'b'], '', 'active').map(task => task.session_id), ['b', 'd'])
  assert.deepEqual(tasks, original)
  assert.deepEqual(selectInboxTasks([], ['missing']), [])
})

test('labels and titles do not invent completion or progress', () => {
  assert.equal(taskTitle(session('one', undefined, { request: '  Investigate\n local  startup  ' })), 'Investigate local startup')
  assert.equal(taskTitle(session('empty', undefined, { request: ' \n ' })), 'Untitled task')
  const title = taskTitle(session('long', 'running', { request: '🤖'.repeat(120) }))
  assert.equal(Array.from(title).length, 112)
  assert.equal(title.endsWith('…'), true)
  assert.equal(taskStatusLabel(session('saved', undefined)), 'Saved')
  assert.equal(taskStatusLabel(session('continue', 'requires_continuation')), 'Needs continuation')
  assert.equal(taskStatusLabel(session('quota', 'quota_exhausted')), 'Quota reached')
  assert.equal(taskModeLabel(session('build', 'running')), 'Build')
})
