'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const {isAllowedEngineRequest, EngineConnection} = require('../engine.cjs');
const {isAllowedApiRequest} = require('../../../desktop/sidecar.cjs');
const ID = 'a'.repeat(32);

test('native harness transport permits read-only memory and snapshot routes only', () => {
  for (const route of ['/memory?limit=50', '/memory/status', '/memory/search?q=target', '/snapshots', `/snapshots/${ID}/diff`, '/language/status']) {
    assert.equal(isAllowedEngineRequest(route, 'GET'), true, route);
    assert.equal(isAllowedEngineRequest(route, 'POST'), false, route);
  }
  for (const route of [`/snapshots/${ID}/restore`, '/memory/index', '/language/context', `/memory/${ID}/delete`, '/jobs/../memory', `/jobs/../snapshots/${ID}/restore`, '/jobs/%2e%2e/memory', '/jobs/%2fmemory', '//other.test/memory', 'https://other.test/memory', '/memory#fragment']) {
    assert.equal(isAllowedEngineRequest(route, 'POST'), false, route);
  }
});

test('native helper routes use the existing authenticated local transport', async () => {
  const requests = [];
  const server = http.createServer((request, response) => {
    requests.push({route: request.url, method: request.method, authorization: request.headers.authorization});
    response.setHeader('Content-Type', 'application/json');
    response.end('{}');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const engine = new EngineConnection({workspace: process.cwd(), sharedURL: `http://127.0.0.1:${server.address().port}`, sharedToken: 'trusted-loopback-token'});
  try {
    for (const route of ['/memory?limit=50', '/snapshots', `/snapshots/${ID}/diff`]) await engine.request(route);
    await assert.rejects(engine.request(`/snapshots/${ID}/restore`, 'POST', {}), /Unsupported/);
    assert.equal(requests.length, 3);
    assert.ok(requests.every(request => request.method === 'GET' && request.authorization === 'Bearer trusted-loopback-token'));
  } finally { await engine.stop(); await new Promise(resolve => server.close(resolve)); }
});

test('desktop bridge allows explicit harness APIs and rejects mismatched methods and unknown routes', () => {
  const reads = ['/memory?limit=50', '/memory/status', '/memory/search?q=context', '/harness', '/snapshots', `/snapshots/${ID}/diff`, '/approvals', '/integrations/mcp', '/integrations/skills', '/integrations/skills/evidence-led-security', '/language/status'];
  const writes = ['/memory/index', `/memory/${ID}/update`, `/memory/${ID}/delete`, '/harness/configure', `/snapshots/${ID}/restore`, `/approvals/${ID}/respond`, '/integrations/mcp/configure', '/integrations/mcp/probe', '/integrations/skills/save', '/language/context', '/language/configure'];
  for (const route of reads) {
    assert.equal(isAllowedApiRequest(route, 'GET'), true, route);
    assert.equal(isAllowedApiRequest(route, 'POST'), false, route);
  }
  for (const route of writes) assert.equal(isAllowedApiRequest(route, 'POST'), true, route);
  for (const route of ['/snapshots/../memory', `/snapshots/${ID}/delete`, '/memory/../settings', '/integrations/mcp/call', '/integrations/skills/../../secret', `/approvals/${ID}/approve`, '/language/execute', '/harness/activate']) {
    assert.equal(isAllowedApiRequest(route, 'POST'), false, route);
    assert.equal(isAllowedApiRequest(route, 'GET'), false, route);
  }
});

test('native bridge allows provider discovery and saved setup without arbitrary engine actions', () => {
  const reads = ['/providers/models?source=openrouter&refresh=1', '/providers/usage', '/providers/quota?refresh=1', '/providers/profiles', '/inference/discovery'];
  const writes = ['/providers/discover', '/providers/profiles/save', `/providers/profiles/${ID}/select`, `/providers/profiles/${ID}/delete`, '/inference/discover', '/inference/discovery/cancel', '/inference/discovery/select'];
  for (const route of reads) {
    assert.equal(isAllowedEngineRequest(route, 'GET'), true, route);
    assert.equal(isAllowedEngineRequest(route, 'POST'), false, route);
  }
  for (const route of writes) {
    assert.equal(isAllowedEngineRequest(route, 'POST'), true, route);
    assert.equal(isAllowedEngineRequest(route, 'GET'), false, route);
  }
  for (const route of ['/providers/profiles/../../settings', '/providers/profiles/%2e%2e/settings', '/providers/discover/extra', `/providers/profiles/${ID.toUpperCase()}/select`, '/inference/discovery/execute', '/inference/discovery/../start', 'https://example.com/providers/models']) {
    assert.equal(isAllowedEngineRequest(route, 'GET'), false, route);
    assert.equal(isAllowedEngineRequest(route, 'POST'), false, route);
  }
  assert.equal(isAllowedEngineRequest('/engagements/defaults', 'GET'), true);
  assert.equal(isAllowedEngineRequest('/engagements/defaults', 'POST'), true);
});

test('native provider routes use the authenticated engine transport', async () => {
  const requests=[];
  const server=http.createServer(async(request,response)=>{
    let raw='';for await(const chunk of request)raw+=chunk;
    requests.push({route:request.url,method:request.method,authorization:request.headers.authorization,body:raw?JSON.parse(raw):undefined});
    response.setHeader('Content-Type','application/json');response.end('{}');
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const engine=new EngineConnection({workspace:process.cwd(),sharedURL:`http://127.0.0.1:${server.address().port}`,sharedToken:'private-native-token'});
  try{
    await engine.request('/providers/models?source=openrouter');
    await engine.request('/providers/quota?refresh=1');
    await engine.request(`/providers/profiles/${ID}/select`,'POST',{});
    await engine.request('/inference/discovery/select','POST',{id:'model-fixture'});
    await engine.request('/engagements/defaults','POST',{settings:{active_http:false}});
    assert.equal(requests.length,5);
    assert.ok(requests.every(item=>item.authorization==='Bearer private-native-token'));
    assert.deepEqual(requests[3].body,{id:'model-fixture'});
    await assert.rejects(engine.request('/providers/profiles/../../settings','POST',{}),/Unsupported/);
    assert.equal(requests.length,5);
  }finally{await engine.stop();await new Promise(resolve=>server.close(resolve));}
});
