const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const Module = require('node:module');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const originalLoad = Module._load;
Module._load = function(id, ...args) {
  if (id === 'electron') return { app: { whenReady: () => new Promise(() => {}), on: () => {} } };
  return originalLoad.call(this, id, ...args);
};
const { installedIDE, spawnIDE } = require('../main.cjs');
Module._load = originalLoad;
test('discovers IDE alongside installed manager without environment override', () => {
  assert.equal(installedIDE({ managerExecutable: '/opt/local-coder/release/manager/local-coder', platform: 'linux', exists: value => value === '/opt/local-coder/release/ide/local-coder-ide' }), '/opt/local-coder/release/ide/local-coder-ide');
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
