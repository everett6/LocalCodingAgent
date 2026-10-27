import test from 'node:test'
import assert from 'node:assert/strict'
import { planDraft, taskObjective } from '../src/workspaceUI.ts'

test('long reviewed plans fit the accepted job request with all file attachments', () => {
  const files = Array.from({length: 12}, (_, i) => `src/${i}/${'a'.repeat(200)}.py`)
  const draft = planDraft('objective '.repeat(2000), 'reviewed plan '.repeat(20000))
  const request = taskObjective(draft, files, 'coding')
  assert.ok(request.length <= 32000, `Request contains ${request.length} characters`)
  assert.match(request, /Original objective: objective/)
  assert.match(request, /Reviewed plan:\nreviewed plan/)
  assert.match(request, /excerpt truncated/)
})
