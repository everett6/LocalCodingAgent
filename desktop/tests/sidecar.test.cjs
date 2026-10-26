const { test } = require('node:test');
const assert = require('node:assert/strict');
const { Sidecar, isAllowedApiRequest } = require('../sidecar.cjs');

test('sidecar parses readiness across output chunks and stops its child', async () => {
  const sidecar = new Sidecar({ command: process.execPath, args: ['-e', 'process.stdout.write("{\\"port\\":"); setTimeout(()=>{process.stdout.write("12345}\\n")},10); setInterval(()=>{},1000)'], timeout: 2000 });
  try { assert.equal(await sidecar.start(), 'http://127.0.0.1:12345'); assert.ok(sidecar.child.pid); }
  finally { await sidecar.stop(); }
  assert.equal(sidecar.child, null);
});
test('failure before readiness rejects clearly', async () => {
  const sidecar = new Sidecar({ command: process.execPath, args: ['-e', 'process.exit(2)'], timeout: 2000 });
  await assert.rejects(sidecar.start(), /exited/); await sidecar.stop();
});
test('startup timeout kills a stuck sidecar', async () => {
  const sidecar = new Sidecar({ command: process.execPath, args: ['-e', 'setInterval(()=>{},1000)'], timeout: 50 });
  await assert.rejects(sidecar.start(), /timed out/); await sidecar.stop(); assert.equal(sidecar.child, null);
});
test('bridge only permits known relative API operations', () => {
  assert.ok(isAllowedApiRequest('/settings', 'POST'));
  assert.ok(isAllowedApiRequest('/events?session_id=x&since=3', 'GET'));
  assert.ok(isAllowedApiRequest('/jobs/session-abc/cancel', 'POST'));
  for (const path of ['https://evil.test/status', '//evil.test/status', '/assets/key', '/file/../../secret', '/status#fragment', '/status\\evil']) assert.equal(isAllowedApiRequest(path, 'GET'), false);
  assert.equal(isAllowedApiRequest('/file?path=x', 'POST'), false);
});
test('stop authenticates and drains the engine before terminating it', async () => {
  const script = `const http = require('node:http');
    const server = http.createServer((req, res) => {
      if (req.url !== '/shutdown' || req.method !== 'POST' || req.headers.authorization !== 'Bearer test-secret') { res.writeHead(403); res.end(); return; }
      process.stdout.write('drained\\n'); res.end('{}');
    });
    server.listen(0, '127.0.0.1', () => console.log(JSON.stringify({port: server.address().port})));`;
  const sidecar = new Sidecar({ command: process.execPath, args: ['-e', script], token: 'test-secret', timeout: 2000 });
  try {
    await sidecar.start();
    let output = '';
    sidecar.child.stdout.on('data', chunk => { output += chunk; });
    await sidecar.stop();
    assert.match(output, /drained/);
  } finally { await sidecar.stop(); }
});
