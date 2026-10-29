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
const { installedIDE, spawnIDE } = require('../main.cjs');
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
