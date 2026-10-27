import test from 'node:test'
import assert from 'node:assert/strict'
import { filterCommands, nextCommandIndex } from '../src/workspaceCommands.ts'

const command = (id, title, extra = {}) => ({ id, title, group: 'Workspace', run() {}, ...extra })

test('search finds title, description and keywords with every query word required', () => {
  const commands = [command('files', 'Open files'), command('memory', 'Memory', { description: 'Inspect saved workspace notes' }),
    command('session', 'Recent session', { keywords: ['conversation', 'history'] })]
  assert.deepEqual(filterCommands(commands, 'saved notes').map(item => item.id), ['memory'])
  assert.deepEqual(filterCommands(commands, 'HISTORY').map(item => item.id), ['session'])
  assert.deepEqual(filterCommands(commands, 'missing').map(item => item.id), [])
})

test('matching ranks exact titles before prefixes, then substrings, then supporting text', () => {
  const commands = [command('description', 'Inspect', { description: 'files' }), command('substring', 'Recent files'),
    command('prefix', 'Files explorer'), command('exact', 'Files')]
  assert.deepEqual(filterCommands(commands, 'files').map(item => item.id), ['exact', 'prefix', 'substring', 'description'])
})

test('empty searches retain curated order; equal matches remain stable', () => {
  const commands = [command('b', 'Find files'), command('a', 'Find files')]
  assert.deepEqual(filterCommands(commands, '  ').map(item => item.id), ['b', 'a'])
  assert.deepEqual(filterCommands(commands, 'find').map(item => item.id), ['b', 'a'])
})

test('search normalizes accents, whitespace and token order', () => {
  const commands = [command('cafe', 'Café workspace', { keywords: ['saved notes'] })]
  assert.equal(filterCommands(commands, '  NOTES   cafe ')[0]?.id, 'cafe')
})

test('results are capped after ranking and inputs are not mutated or executed', () => {
  let calls = 0
  const commands = Array.from({ length: 75 }, (_, index) => command(String(index), `File ${index}`, { run: () => calls++ }))
  commands.push(command('exact', 'File', { run: () => calls++ }))
  const snapshot = [...commands]
  const result = filterCommands(commands, 'file')
  assert.equal(result.length, 50)
  assert.equal(result[0].id, 'exact')
  assert.deepEqual(commands, snapshot)
  assert.equal(calls, 0)
})

test('keyboard movement wraps and skips disabled commands in either direction', () => {
  const commands = [command('one', 'First'), command('disabled', 'Unavailable', { disabled: true }), command('last', 'Last')]
  assert.equal(nextCommandIndex(commands, 0, 1), 2)
  assert.equal(nextCommandIndex(commands, 2, 1), 0)
  assert.equal(nextCommandIndex(commands, 0, -1), 2)
  assert.equal(nextCommandIndex(commands, 2, -1), 0)
  assert.equal(nextCommandIndex(commands, -1, 1), 0)
  assert.equal(nextCommandIndex(commands, -1, -1), 2)
})

test('empty or fully disabled lists have no executable selection', () => {
  assert.equal(nextCommandIndex([], -1, 1), -1)
  assert.equal(nextCommandIndex([command('no', 'Unavailable', { disabled: true })], 0, 1), -1)
})
