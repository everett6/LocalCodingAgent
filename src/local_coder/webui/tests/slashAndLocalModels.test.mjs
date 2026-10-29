import test from 'node:test'
import assert from 'node:assert/strict'
import { matchingSlashCommands, parseSlashCommand } from '../src/slashCommands.ts'
import { modelName, modelQuantization, runtimePresentation } from '../src/localModels.ts'

test('slash commands preserve prompt arguments and reject unknown command names', () => {
  assert.deepEqual(parseSlashCommand('/STATUS'), { name: 'status', argument: '' })
  assert.deepEqual(parseSlashCommand('/plan repair parser\nkeep the API'), { name: 'plan', argument: 'repair parser\nkeep the API' })
  assert.deepEqual(parseSlashCommand('/invented-command'), { name: null, argument: '' })
  assert.equal(parseSlashCommand('/home/user/source.py'), null)
  assert.equal(parseSlashCommand('Review /status in the code'), null)
})

test('slash menu filters names only and does not mistake task prose for a command', () => {
  assert.ok(matchingSlashCommands('/').length >= 10)
  assert.deepEqual(matchingSlashCommands('/sta').map(item => item.name), ['status'])
  assert.deepEqual(matchingSlashCommands('/plan change the API'), [])
  assert.deepEqual(matchingSlashCommands('ordinary prompt'), [])
})

test('local library cannot promote a selected draft or failed startup to loaded', () => {
  assert.equal(runtimePresentation('stopped', false, '/models/selected.gguf').label, 'Not loaded')
  assert.equal(runtimePresentation('starting', false, '/models/selected.gguf').label, 'Loading model')
  assert.equal(runtimePresentation('error', false, '/models/failed.gguf').label, 'Needs attention')
  const running = runtimePresentation('running', true, '/models/actually-loaded.gguf')
  assert.equal(running.label, 'Loaded')
  assert.equal(running.model, 'actually-loaded.gguf')
  assert.equal(modelName('C:\\Models\\Windows.gguf'), 'Windows.gguf')
  assert.equal(modelQuantization('/models/Qwen-UD-IQ2_XXS.gguf'), 'IQ2_XXS')
  assert.equal(modelQuantization('/models/Qwen-Q4_K_M.gguf'), 'Q4_K_M')
  assert.equal(modelQuantization('/models/model.gguf'), null)
})
