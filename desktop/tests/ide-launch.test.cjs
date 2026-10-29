const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const Module = require('node:module');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const originalLoad = Module._load;
Module._load = function(id, ...args) {
  if (id === 'electron') return { app: { getPath: () => '/legacy-profile', setPath() {}, setName() {}, whenReady: () => new Promise(() => {}), on: () => {} } };
  return originalLoad.call(this, id, ...args);
};
const { installedIDE, spawnIDE, startWithRecovery } = require('../main.cjs');
Module._load = originalLoad;
test('discovers IDE alongside installed manager without environment override', () => {
  const release = path.resolve(os.tmpdir(), 'local-coder-release-fixture');
  for (const [platform, relative] of [['linux', 'local-coder-ide'], ['win32', 'Local Coder IDE.exe'], ['darwin', 'Local Coder IDE.app/Contents/MacOS/Local Coder IDE']]) {
    const expected = path.join(release, 'ide', relative);
    assert.equal(installedIDE({ managerExecutable: path.join(release, 'manager', 'local-coder'), platform, exists: value => value === expected }), expected);
  }
});
test('detects native startup failure after spawn and preserves private diagnostics', async () => {
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), 'local-coder-launch-'));
  const logPath = path.join(folder, 'logs', 'ide.log');
  try {
    await assert.rejects(spawnIDE(process.execPath, ['-e', 'console.error("native startup failed"); process.exit(17)'], { env: process.env, logPath, startupMs: 2000 }), /exit 17.*Startup diagnostics/);
    assert.match(fs.readFileSync(logPath, 'utf8'), /native startup failed/);
    if (process.platform !== 'win32') assert.equal(fs.statSync(logPath).mode & 0o777, 0o600);
  } finally { fs.rmSync(folder, { recursive: true, force: true }); }
});
test('desktop retries an unknown transient engine startup exit only once', async () => {
  const transient = Object.assign(new Error('fixture startup failure'), {code: 'engine_exited', retryable: true});
  let calls = 0, retries = 0;
  const engine = {start: async () => { calls++; if (calls === 1) throw transient; return 'http://127.0.0.1:12345'; }};
  assert.equal(await startWithRecovery(engine, {delay: async () => {}, onRetry: () => retries++}), 'http://127.0.0.1:12345');
  assert.equal(calls, 2); assert.equal(retries, 1);
  calls = 0;
  await assert.rejects(startWithRecovery({start: async () => { calls++; throw transient; }}, {delay: async () => {}}), error => error === transient);
  assert.equal(calls, 2);
});
test('desktop does not retry a dependency failure, cancellation or an unclassified error', async () => {
  for (const error of [new Error('unknown'), Object.assign(new Error('dependency'), {code: 'engine_dependency_missing', retryable: false})]) {
    let calls = 0;
    await assert.rejects(startWithRecovery({start: async () => { calls++; throw error; }}), value => value === error);
    assert.equal(calls, 1);
  }
  const error = Object.assign(new Error('transient'), {code: 'engine_exited', retryable: true});
  let calls = 0, cancelled = false;
  await assert.rejects(startWithRecovery({start: async () => { calls++; throw error; }}, {cancelled: () => cancelled, delay: async () => {cancelled = true;}}), value => value === error);
  assert.equal(calls, 1);
});
