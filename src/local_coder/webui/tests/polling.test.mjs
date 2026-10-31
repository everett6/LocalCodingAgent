import test from 'node:test'
import assert from 'node:assert/strict'
import { setTimeout as delay } from 'node:timers/promises'
import { startSerialPolling } from '../src/polling.ts'

function deferred() { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }

test('a slow local refresh cannot overlap with another scheduled refresh', async context => {
  const first = deferred(), second = deferred()
  let calls = 0, active = 0, maximum = 0
  const stop = startSerialPolling(async () => {
    calls += 1; active += 1; maximum = Math.max(maximum, active)
    try { if (calls === 1) await first.promise; else second.resolve() }
    finally { active -= 1 }
  }, 1, error => assert.fail(String(error)))
  context.after(stop)
  await delay(15)
  assert.equal(calls, 1)
  first.resolve()
  await Promise.race([second.promise, delay(1000).then(() => assert.fail('The next refresh was never scheduled'))])
  stop()
  assert.equal(maximum, 1)
  assert.equal(active, 0)
})

test('closing a view aborts its active refresh and prevents later refreshes', async () => {
  const pending = deferred()
  let signal, calls = 0
  const stop = startSerialPolling(async current => { signal = current; calls += 1; await pending.promise }, 1, error => assert.fail(String(error)))
  assert.equal(signal.aborted, false)
  stop(); stop()
  assert.equal(signal.aborted, true)
  pending.resolve()
  await delay(10)
  assert.equal(calls, 1)
})

test('transient refresh errors are reported and polling can recover', async context => {
  const recovered = deferred(), errors = []
  let calls = 0
  const stop = startSerialPolling(async () => {
    calls += 1
    if (calls === 1) throw new Error('Temporary failure')
    recovered.resolve()
  }, 1, error => errors.push(error.message))
  context.after(stop)
  await Promise.race([recovered.promise, delay(1000).then(() => assert.fail('Polling did not recover'))])
  stop()
  assert.deepEqual(errors, ['Temporary failure'])
  assert.equal(calls, 2)
})
