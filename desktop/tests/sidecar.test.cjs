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
  assert.ok(isAllowedApiRequest('/engagements', 'POST'));
  assert.ok(isAllowedApiRequest('/engagements/eng-0123456789ab/report', 'GET'));
  assert.equal(isAllowedApiRequest('/engagements/../outside', 'GET'),false);
  assert.equal(isAllowedApiRequest('/engagements/eng-0123456789ab', 'POST'),false);
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

test('manager attaches to authenticated workspace engine without spawning or stopping it', async () => {
  const fs=require('node:fs'),os=require('node:os'),path=require('node:path'),http=require('node:http'),crypto=require('node:crypto');
  const workspace=fs.mkdtempSync(path.join(os.tmpdir(),'manager-attach-'));
  const directory=path.join(workspace,'connections');fs.mkdirSync(directory,{mode:0o700});
  let requests=0;
  const server=http.createServer((req,res)=>{requests++;assert.equal(req.headers.authorization,'Bearer shared-test');res.end(JSON.stringify({project_root:workspace,sessions:[]}));});
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const origin=`http://127.0.0.1:${server.address().port}`;
  const key=crypto.createHash('sha256').update(workspace).digest('hex');
  fs.writeFileSync(path.join(directory,key+'.json'),JSON.stringify({workspace,origin,token:'shared-test',pid:process.pid}),{mode:0o600});
  const sidecar=new Sidecar({command:'/must-not-spawn',cwd:workspace,token:'fresh-token',discoveryDirectory:directory,timeout:300});
  try {assert.equal(await sidecar.start(),origin);assert.equal(sidecar.token,'shared-test');assert.equal(sidecar.child,null);await sidecar.stop();assert.equal(server.listening,true);assert.equal(requests,1);}
  finally {await sidecar.stop();await new Promise(resolve=>server.close(resolve));fs.rmSync(workspace,{recursive:true,force:true});}
});
