import test from 'node:test'
import assert from 'node:assert/strict'
import { canUndoSession } from '../src/api.ts'

const session = { session_id: 'task-a', request: 'Update source', phase: 'run', status: 'completed', pre_run_checkpoint_id: 'checkpoint-a' }

test('task undo requires its own checkpoint and a finished current workspace job', () => {
  assert.equal(canUndoSession(session), true)
  for (const status of ['completed', 'cancelled', 'failed']) assert.equal(canUndoSession({ ...session, status }), true)
  for (const status of ['queued', 'running', 'cancelling', 'interrupted', 'quota_exhausted', undefined]) assert.equal(canUndoSession({ ...session, status }), false)
  assert.equal(canUndoSession(undefined), false)
  assert.equal(canUndoSession({ ...session, pre_run_checkpoint_id: undefined }), false)
  assert.equal(canUndoSession(session, true), false)
})

test('a restored or automatically rolled back task cannot offer another undo', () => {
  assert.equal(canUndoSession({ ...session, undo_applied: true }), false)
  assert.equal(canUndoSession({ ...session, rollback_applied: true }), false)
  assert.equal(canUndoSession({ ...session, undo_applied: false, rollback_applied: false }), true)
})
