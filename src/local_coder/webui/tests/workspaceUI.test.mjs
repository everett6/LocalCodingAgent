import test from 'node:test'
import assert from 'node:assert/strict'
import { isContextPath, taskObjective, planDraft, validPlanTasks } from '../src/workspaceUI.ts'

test('file context accepts local source and rejects private or ambiguous paths', () => {
  for (const path of ['src/app.ts', 'tests/test_api.py', 'src/数据.py']) assert.equal(isContextPath(path), true)
  for (const path of ['../outside.py', '/etc/passwd', 'C:\\secret.py', '.env', 'src/.env.local', '.ssh/id_rsa', 'app/credentials.json', 'secret.pem', '.local-coder/sessions.json', 'src/a b.py', 'src/a\n.py']) assert.equal(isContextPath(path), false, path)
})
test('context is deduplicated and bounded; scoped red-team sources stay in engagement tools', () => {
  const paths = ['src/app.ts', 'src/app.ts', '.env', ...Array.from({length:20}, (_,i)=>`src/${i}.py`)]
  const output = taskObjective('Inspect this', paths, 'coding')
  assert.equal((output.match(/@src\/app.ts/g)||[]).length, 1)
  assert.equal((output.match(/@/g)||[]).length, 12)
  assert.equal(output.includes('.env'), false)
  assert.equal(taskObjective('Inspect this', paths, 'red_team'), 'Inspect this')
})

test('plan handoff carries the actual report into a bounded draft', () => {
  const draft = planDraft('Add validation', 'Check the function boundary first.')
  assert.match(draft, /Add validation/)
  assert.match(draft, /Check the function boundary first/)
  const long = planDraft('x'.repeat(20000), 'y'.repeat(200000))
  assert.ok(long.length < 100000)
  assert.match(long, /excerpt truncated/)
})
test('malformed plan entries cannot reach React rendering', () => {
  assert.deepEqual(validPlanTasks([null, 3, {task_id:'a',objective:{}}, {task_id:'b',objective:'Valid',status:[]}]), [])
  assert.deepEqual(validPlanTasks([{task_id:'a',objective:'Valid'}]), [{task_id:'a',objective:'Valid'}])
  assert.equal(validPlanTasks(Array.from({length:300},(_,i)=>({task_id:String(i),objective:'Step'}))).length, 200)
})
